import gin

import numpy as np

import torch
import torch.nn as nn
import lightning.pytorch as L

from model.DANEncoder import Encoder
from model.ConvNextEncoder import ConvNextEncoder
from model.DANDecoder import Decoder
from torchinfo import summary
from eval_functions import compute_poliphony_metrics
from torchmetrics.text.perplexity import Perplexity

class PositionalEncoding2D(nn.Module):

    def __init__(self, dim, h_max, w_max):
        super(PositionalEncoding2D, self).__init__()
        self.h_max = h_max
        self.max_w = w_max
        self.dim = dim
        self.pe = torch.zeros((1, dim, h_max, w_max), device=torch.device('cuda' if torch.cuda.is_available() else 'cpu'), requires_grad=False)

        div = torch.exp(-torch.arange(0., dim // 2, 2) / dim * torch.log(torch.tensor(10000.0))).unsqueeze(1)
        w_pos = torch.arange(0., w_max)
        h_pos = torch.arange(0., h_max)
        self.pe[:, :dim // 2:2, :, :] = torch.sin(h_pos * div).unsqueeze(0).unsqueeze(3).repeat(1, 1, 1, w_max)
        self.pe[:, 1:dim // 2:2, :, :] = torch.cos(h_pos * div).unsqueeze(0).unsqueeze(3).repeat(1, 1, 1, w_max)
        self.pe[:, dim // 2::2, :, :] = torch.sin(w_pos * div).unsqueeze(0).unsqueeze(2).repeat(1, 1, h_max, 1)
        self.pe[:, dim // 2 + 1::2, :, :] = torch.cos(w_pos * div).unsqueeze(0).unsqueeze(2).repeat(1, 1, h_max, 1)

    def forward(self, x):
        """
        Add 2D positional encoding to x
        x: (B, C, H, W)
        """
        return x + self.pe[:, :, :x.size(2), :x.size(3)]

    def get_pe_by_size(self, h, w, device):
        return self.pe[:, :, :h, :w].to(device)

@gin.configurable
class DAN(L.LightningModule):
    def __init__(self, maxh, maxw, maxlen, out_categories, padding_token, in_channels, w2i, i2w, out_dir, d_model=None, dim_ff=None, num_dec_layers=None, encoder_type="Normal") -> None:
        super().__init__()
        
        if encoder_type == "NexT":
            self.encoder = ConvNextEncoder(in_chans=1, depths=[3,3,9], dims=[64, 128, 256])
        else:
            self.encoder = Encoder(in_channels=in_channels)

        self.decoder = Decoder(d_model, dim_ff, num_dec_layers, maxlen, out_categories)
        self.positional_2D = PositionalEncoding2D(d_model, maxh, maxw)

        self.padding_token = padding_token

        self.loss = nn.CrossEntropyLoss(ignore_index=self.padding_token)

        self.eximgs = []
        self.expreds = []
        self.exgts = []

        self.valpredictions = []
        self.valgts = []

        self.w2i = w2i
        self.i2w = i2w
        self.maxlen = maxlen
        self.out_dir=out_dir

        self.save_hyperparameters()

    def forward(self, x, y_pred):
        encoder_output = self.encoder(x)
        b, c, h, w = encoder_output.size()
        reduced_size = [s.shape[:2] for s in encoder_output]
        ylens = [len(sample) for sample in y_pred]
        cache = None

        pos_features = self.positional_2D(encoder_output)
        features = torch.flatten(encoder_output, start_dim=2, end_dim=3).permute(2,0,1)
        enhanced_features = features
        enhanced_features = torch.flatten(pos_features, start_dim=2, end_dim=3).permute(2,0,1)
        output, predictions, _, _, weights = self.decoder(features, enhanced_features, y_pred[:, :-1], reduced_size, 
                                                           [max(ylens) for _ in range(b)], encoder_output.size(), 
                                                           start=0, cache=cache, keep_all_weights=True)
    
        return output, predictions, cache, weights


    def forward_encoder(self, x):
        return self.encoder(x)
    
    def forward_decoder(self, encoder_output, last_preds, cache=None):
        b, c, h, w = encoder_output.size()
        reduced_size = [s.shape[:2] for s in encoder_output]
        ylens = [len(sample) for sample in last_preds]
        cache = cache

        pos_features = self.positional_2D(encoder_output)
        features = torch.flatten(encoder_output, start_dim=2, end_dim=3).permute(2,0,1)
        enhanced_features = features
        enhanced_features = torch.flatten(pos_features, start_dim=2, end_dim=3).permute(2,0,1)
        output, predictions, _, _, weights = self.decoder(features, enhanced_features, last_preds[:, :], reduced_size, 
                                                           [max(ylens) for _ in range(b)], encoder_output.size(), 
                                                           start=0, cache=cache, keep_all_weights=True)
    
        return output, predictions, cache, weights
    
    def configure_optimizers(self):
        return torch.optim.Adam(list(self.encoder.parameters()) + list(self.decoder.parameters()), lr=1e-4, amsgrad=False)

    def training_step(self, train_batch):
        x, di, y = train_batch
        output, predictions, cache, weights = self.forward(x, di)
        loss = self.loss(predictions, y[:, :-1])
        self.log('loss', loss, on_epoch=True, batch_size=1, prog_bar=True)
        return loss

    def validation_step(self, val_batch, batch_idx):
        device=torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        x, _, y = val_batch
        encoder_output = self.forward_encoder(x)
        predicted_sequence = torch.from_numpy(np.asarray([self.w2i['<bos>']])).to(device).unsqueeze(0)
        cache = None
        for i in range(self.maxlen):
             output, predictions, cache, weights = self.forward_decoder(encoder_output, predicted_sequence.long(), cache=cache)
             predicted_token = torch.argmax(predictions[:, :, -1]).item()
             predicted_sequence = torch.cat([predicted_sequence, torch.argmax(predictions[:, :, -1], dim=1, keepdim=True)], dim=1)
             if predicted_token == self.w2i['<eos>']:
                 break
        
        dec = "".join([self.i2w[token.item()] for token in predicted_sequence.squeeze(0)[1:]])
        dec = dec.replace("<t>", "\t")
        dec = dec.replace("<b>", "\n")
        dec = dec.replace("<s>", " ")

        gt = "".join([self.i2w[token.item()] for token in y.squeeze(0)[:-1]])
        gt = gt.replace("<t>", "\t")
        gt = gt.replace("<b>", "\n")
        gt = gt.replace("<s>", " ")

        self.valpredictions.append(dec)
        self.valgts.append(gt)
    
    
    def test_step(self, test_batch, batch_idx):
        device=torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        x, _, y = test_batch
        encoder_output = self.forward_encoder(x)
        predicted_sequence = torch.from_numpy(np.asarray([self.w2i['<bos>']])).to(device).unsqueeze(0)
        cache = None
        for i in range(self.maxlen):
             output, predictions, cache, weights = self.forward_decoder(encoder_output, predicted_sequence.long(), cache=cache)
             predicted_token = torch.argmax(predictions[:, :, -1]).item()
             predicted_sequence = torch.cat([predicted_sequence, torch.argmax(predictions[:, :, -1], dim=1, keepdim=True)], dim=1)
             if predicted_token == self.w2i['<eos>']:
                 break
        
        dec = "".join([self.i2w[token.item()] for token in predicted_sequence.squeeze(0)[1:]])
        dec = dec.replace("<t>", "\t")
        dec = dec.replace("<b>", "\n")
        dec = dec.replace("<s>", " ")

        gt = "".join([self.i2w[token.item()] for token in y.squeeze(0)[:-1]])
        gt = gt.replace("<t>", "\t")
        gt = gt.replace("<b>", "\n")
        gt = gt.replace("<s>", " ")

        self.valpredictions.append(dec)
        self.valgts.append(gt)

class Poliphony_DAN(DAN):
    def __init__(self, maxh, maxw, maxlen, out_categories, padding_token, in_channels, w2i, i2w, out_dir) -> None:
        super().__init__(maxh, maxw, maxlen, out_categories, padding_token, in_channels, w2i, i2w, out_dir)
    
    def on_validation_epoch_end(self):
        cer, ser, ler = compute_poliphony_metrics(self.valpredictions, self.valgts)
        
        random_index = np.random.randint(0, len(self.valpredictions))
        predtoshow = self.valpredictions[random_index]
        gttoshow = self.valgts[random_index]
        print(f"[Prediction] - {predtoshow}")
        print(f"[GT] - {gttoshow}")

        self.log('val_CER', cer, prog_bar=True)
        self.log('val_SER', ser, prog_bar=True)
        self.log('val_LER', ler, prog_bar=True)

        self.valpredictions = []
        self.valgts = []

        return ser

    def on_test_epoch_end(self):
        cer, ser, ler = compute_poliphony_metrics(self.valpredictions, self.valgts)

        #for index, sample in enumerate(self.valpredictions):
        #    with open(f"{self.out_dir}/hyp/{index}.krn", "w+") as krnfile:
        #        krnfile.write(sample)
        #
        #for index, sample in enumerate(self.valgts):
        #    with open(f"{self.out_dir}/gt/{index}.krn", "w+") as krnfile:
        #        krnfile.write(sample)

        self.log('test_CER', cer)
        self.log('test_SER', ser)
        self.log('test_LER', ler)

        self.valpredictions = []
        self.valgts = []

        return ser

@gin.configurable
class DANLM(L.LightningModule):
    def __init__(self, d_model, dim_ff, maxlen, num_dec_layers, out_categories, padding_token, w2i, i2w, out_dir):
        super().__init__()
        self.decoder = Decoder(d_model, dim_ff, num_dec_layers, maxlen, out_categories)

        self.padding_token = padding_token

        self.loss = nn.CrossEntropyLoss(ignore_index=self.padding_token)

        self.valprobs = []
        self.valids = []

        self.w2i = w2i
        self.i2w = i2w

        self.maxlen = maxlen
        self.out_dir=out_dir

        self.decoder.set_lm_mode()

        self.perplexities = []
        self.perplexity_metric = Perplexity(ignore_index=padding_token)

        self.save_hyperparameters()

    def forward(self, y_pred):
        return self.forward_decoder(y_pred, cache=None)
    
    def forward_decoder(self, last_preds, cache=None):
        b, l = last_preds.size()
        ylens = [len(sample) for sample in last_preds]
        cache = cache
        output, predictions, _, _ = self.decoder.forward_lm(last_preds[:, :],
                                                          [max(ylens) for _ in range(b)],
                                                          start=0, cache=cache, keep_all_weights=True)
    
        return output, predictions, cache
    
    def configure_optimizers(self):
        return torch.optim.Adam(self.decoder.parameters(), lr=0.0001, amsgrad=False)

    def training_step(self, train_batch):
        _, di, y = train_batch
        output, predictions, cache = self.forward(di)
    
        loss = self.loss(predictions, y)
        
        self.perplexities.append(self.perplexity_metric(predictions.permute(0,2,1).contiguous(), y).item())

        self.log("loss", loss, on_epoch=True, batch_size=1, prog_bar=True)
        return loss
    
    def on_train_epoch_end(self) -> None:
        mean_perplexity = np.average(self.perplexities)
        self.perplexities = []
        self.log("train_perp", mean_perplexity, prog_bar=True)
        return mean_perplexity
    
    def validation_step(self, val_batch, batch_idx):
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        _, _, y = val_batch
        cache = None
        probabilities = []
        y = y.squeeze(0)

        predicted_sequence = torch.from_numpy(np.asarray([self.w2i['<bos>']])).to(device).unsqueeze(0)

        for i in range(len(y)):
            output, predictions, cache = self.forward_decoder(predicted_sequence.long(), cache=cache)
            probabilities.append(predictions[:, :, -1].cpu().detach())
            predicted_sequence = torch.cat([predicted_sequence, y[i].unsqueeze(0).unsqueeze(0)], dim=1)

        self.perplexities.append(self.perplexity_metric(torch.cat(probabilities, dim=0).unsqueeze(0), y.unsqueeze(0).cpu()).item())

    def on_validation_epoch_end(self, name="val"):
        mean_perplexity = np.average(self.perplexities)
        self.log(f"{name}_perp", mean_perplexity, prog_bar=True)
        self.perplexities = []
        return mean_perplexity
    
    def on_test_epoch_end(self):
        return self.on_validation_epoch_end(name="test")
    
    def test_step(self, test_batch, batch_idx):
        self.validation_step(test_batch, batch_idx)
    
    def get_dictionaries(self):
        return self.w2i, self.i2w
    
def get_DAN_network(in_channels, max_height, max_width, max_len, out_categories, w2i, i2w, out_dir, model_name=None):
    device=torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model = Poliphony_DAN(in_channels=in_channels, maxh=(max_height//16)+1, maxw=(max_width//8)+1, 
                maxlen=max_len+1, out_categories=out_categories, 
                padding_token=0, w2i=w2i, i2w=i2w, out_dir=out_dir).to(device)
    
    #with torch.no_grad():
    #    print(max_height, max_width, max_len)
    #    _ = model(torch.randn((1,1,max_height,max_width), device=device), torch.randint(low=0, high=100,size=(1,max_len), device=device).long())
    #import sys
    #sys.exit()
    summary(model, input_size=[(1,1,max_height,max_width), (1,max_len)], dtypes=[torch.float, torch.long])

    return model

def get_DAN_LM_network(d_model, dim_ff, max_len, out_categories, w2i, i2w, out_dir, model_name=None):
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model = DANLM(d_model=d_model, num_dec_layers=8, 
                          dim_ff=dim_ff, maxlen=max_len+1, out_categories=out_categories, 
                          padding_token=w2i["<pad>"], w2i=w2i, i2w=i2w, out_dir=out_dir).to(device)
    #with torch.no_grad():
    #    _ = model(torch.randint(low=0, high=100,size=(1,max_len), device=device).long())
    
    summary(model, input_size=[(1, max_len)], dtypes=[torch.long])
    return model
