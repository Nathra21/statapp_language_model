import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
import numpy as np
import math
import sys
sys.path.append("..")
from common import get_positional_encodings

# Hyperparamètres

nb_decoders = 2
vector_size = 64
nb_heads = 4
head_size = vector_size//nb_heads
max_length = 8
ffn_hidden_size = 256 #vector_size*4 pour gpt-2
vocab_size = 1000

device = "cuda:0" 


class Transformer(nn.Module):
    "Whole transformer structure composed of stacked decoder blocks"
    def __init__(self, vector_size, vocab_size, nb_decoders, decoder):
        #Intégrer nb_decoders comme input
        super(Transformer, self).__init__()
        self.vector_size = vector_size
        self.nb_decoders = nb_decoders
        self.decoders = nn.ModuleList([decoder for i in range(self.nb_decoders)])
        self.vocab_size = vocab_size
        self.embedding = nn.Embedding(self.vocab_size, self.vector_size)
        self.finalfc = nn.Linear(self.vector_size, self.vocab_size)

    def forward(self, x):
        
        embedded = self.embedding(x)
        seq_length = embedded.shape[-2]
        #if USE_CUDA:
        pos_encodings = (torch.tensor(get_positional_encodings(seq_length, self.vector_size))).to(device)
        x = (torch.tensor(torch.add(embedded, pos_encodings), dtype=torch.float32)).to(device)
            
        for decoder in self.decoders:
            x = decoder(x)
            
        x = F.log_softmax(self.finalfc(x), dim=-1)
        return x

    
class Decoder(nn.Module):
    "Decoder block applying multihead attention mechanism followed by a feedforward network"
    def __init__(self, vector_size, multihead_attention, feedforward_network):
        super(Decoder, self).__init__()
        self.vector_size = vector_size
        self.multihead_attention = multihead_attention
        self.feedforward_network = feedforward_network
        self.layernorma = nn.modules.normalization.LayerNorm(self.vector_size)
        

    def forward(self, x):
        #normalisation a la fin ou au debut ?
        
        #if USE_CUDA:
        mha = self.multihead_attention(self.layernorma(x))
        x = (torch.add(x,mha)).to(device)
        ffo = self.feedforward_network(self.layernorma(x))
        x = (torch.add(x, ffo)).to(device)

        return x

    
class FeedforwardNetwork(nn.Module):
    "Classic Feedforward Network with two layers"
    def __init__(self, vector_size, hidden_size):
        super(FeedforwardNetwork, self).__init__()
        self.hidden_size  = hidden_size
        self.vector_size = vector_size
        self.fc1 = nn.Linear(vector_size, self.hidden_size)
        self.fc2 = nn.Linear(self.hidden_size, self.vector_size)
        
    def forward(self, x):
        x = F.relu(self.fc1(x))
        x = self.fc2(x)
        return x
    

class MultiHeadAttention(nn.Module):
    "MultiHead Attention Block"
    def __init__(self, vector_size, nb_heads, head_size):
        super(MultiHeadAttention, self).__init__()
        self.vector_size = vector_size
        self.nb_heads = nb_heads
        self.head_size = head_size
        #bias = False ? (pour équivalence stricte avec une multiplication matricielle)
        self.w_q = nn.Linear(self.vector_size, self.vector_size)
        self.w_k = nn.Linear(self.vector_size, self.vector_size)
        self.w_v = nn.Linear(self.vector_size, self.vector_size)
        self.w_0 = nn.Linear(self.vector_size, self.vector_size)
        
    def attention_mask(self, w):
        #Mask matrix
        #if USE_CUDA:
        mask = (torch.triu( torch.full((w.shape[-1],w.shape[-1]),(-math.inf)), diagonal=1)).to(device)
        w_masked = (torch.add(w, mask)).to(device)
        
        return w_masked
    
    def reshape_w(self, w):
        #reshape a matrix (batch_size, nb_inputs, vector_size)
        #towards a matrix (batch_size, nb_heads, nb_inputs, head_size)
        #if USE_CUDA:
        w = (w.reshape(-1, w.shape[1], self.nb_heads, self.head_size)).to(device)
        w = (w.transpose(-2,-3)).to(device)
        return w
        
    def forward(self, x):
        
        # x size (batch_size, nb_inputs, vector_size)
        # self.w_q(x) size (batch_size, nb_inputs, vector_size)
        q = self.reshape_w(self.w_q(x))
        # q size (batch_size, nb_heads, nb_inputs, head_size)
        k = self.reshape_w(self.w_k(x))
        # k size (batch_size, nb_heads, nb_inputs, head_size)
        v = self.reshape_w(self.w_v(x))
        # v size (batch_size, nb_heads, nb_inputs, head_size)
        
        w = (torch.matmul(q, k.transpose(-1,-2)) / math.sqrt(k.shape[-1])).to(device)
        w = (torch.softmax(self.attention_mask(w), dim=-1)).to(device)
        # w size (batch_size, nb_heads, nb_inputs, nb_inputs)
        
        a = (torch.matmul(w, v)).to(device)
        # a size (batch_size, nb_heads, nb_inputs, head_size)
        
        a = a.transpose(-2,-3).reshape(-1, a.shape[-2], self.vector_size)
        # a size (batch_size, nb_inputs, vector_size) (concatenation of multihead matrices)
        
        a = self.w_0(a)
        # a size (batch_size, nb_inputs, vector_size)

        return a
    
def buildTransformer(vector_size, nb_decoders, nb_heads, head_size, ffn_hidden_size, vocab_size):
    LMtransformer = (Transformer(vector_size, vocab_size, nb_decoders, Decoder(vector_size, MultiHeadAttention(vector_size, nb_heads, head_size), FeedforwardNetwork(vector_size, ffn_hidden_size)))).to(device)
    return LMtransformer