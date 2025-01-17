import sys
import math
import random
import gzip
import pandas as pd
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
from sklearn.preprocessing import LabelEncoder
from sklearn import metrics, calibration
from scipy.special import lambertw

from MuRaL.nn_utils import *
from MuRaL.evaluation import *

class FeedForwardNN(nn.Module):
    """Feedforward only model with local data"""

    def __init__(self, emb_dims, no_of_cont, lin_layer_sizes, emb_dropout, lin_layer_dropouts, n_class, emb_padding_idx=None):
        """  
        Args:
            emb_dims: embedding dimensions
            no_of_cont: number of continuous features
            lin_layer_sizes: sizes of linear layers
            emb_dropout: dropout for the embedding layer
            lin_layer_dropouts: dropouts for linear layers
            n_class: number of classes (labels)
            emb_padding_idx: number to be used for padding in embeddings
        """
        super(FeedForwardNN, self).__init__()
        
        self.n_class = n_class
        
        # FeedForward layers for local input
        # Embedding layers
        print('emb_dims: ', emb_dims)
        print('emb_padding_idx: ', emb_padding_idx)
        self.no_of_cat = len(emb_dims)
        
        #self.emb_layers = nn.ModuleList([nn.Embedding(emb_padding_idx+1, y, padding_idx = emb_padding_idx) for x, y in emb_dims])
        self.emb_layer = nn.Embedding(emb_padding_idx+1, 5)

        #no_of_embs = sum([y for x, y in emb_dims])
        self.no_of_embs = len(emb_dims)*5
        self.no_of_cont = no_of_cont

        # Linear Layers
        first_lin_layer = nn.Linear(self.no_of_embs + self.no_of_cont, lin_layer_sizes[0])

        self.lin_layers = nn.ModuleList([first_lin_layer] + [nn.Linear(lin_layer_sizes[i], lin_layer_sizes[i + 1]) for i in range(len(lin_layer_sizes) - 1)])

        # Batch Norm Layers
        self.first_bn_layer = nn.BatchNorm1d(self.no_of_cont)
        self.bn_layers = nn.ModuleList([nn.BatchNorm1d(size) for size in lin_layer_sizes])

        # Dropout Layers
        self.emb_dropout_layer = nn.Dropout(emb_dropout)
        self.droput_layers = nn.ModuleList([nn.Dropout(size) for size in lin_layer_dropouts])
        
        # Output Layer
        self.output_layer = nn.Linear(lin_layer_sizes[-1], n_class)

    def forward(self, cont_data, cat_data):
        """
        Forward pass
        
        Args:
            cont_data: continuous data
            cat_data: categorical seq data
        """
        if self.no_of_embs != 0:
            local_out = [self.emb_layer(cat_data[:, i]) for i in range(self.no_of_cat)]
            
        local_out = torch.cat(local_out, dim = 1) #x.shape: batch_size * sum(emb_size)
        local_out = self.emb_dropout_layer(local_out)

        if self.no_of_cont != 0:
            normalized_cont_data = self.first_bn_layer(cont_data)

            if self.no_of_embs != 0:
                local_out = torch.cat([local_out, normalized_cont_data], dim = 1) 
            else:
                local_out = normalized_cont_data
        
        for lin_layer, dropout_layer, bn_layer in zip(self.lin_layers, self.droput_layers, self.bn_layers):
            local_out = F.relu(lin_layer(local_out))
            local_out = bn_layer(local_out)
            local_out = dropout_layer(local_out)
        
        out = self.output_layer(local_out)
        
        return out

class Network0(nn.Module):
    """Wrapper for Feedforward only model with local data"""
    def __init__(self, emb_dims, no_of_cont, lin_layer_sizes, emb_dropout, lin_layer_dropouts, n_class, emb_padding_idx=None):
        
        super(Network0, self).__init__()
        self.model = FeedForwardNN(emb_dims, no_of_cont, lin_layer_sizes, emb_dropout, lin_layer_dropouts, n_class, emb_padding_idx)
    
    def forward(self, local_input, distal_input=None):
        """Write this for using the same functional interface when doing forward pass"""
        cont_data, cat_data = local_input
        
        return self.model.forward(cont_data, cat_data)
        
    
class Network1(nn.Module):
    """The expanded-only model"""
    def __init__(self,  in_channels, out_channels, kernel_size, distal_radius, distal_order, distal_fc_dropout, n_class):
        """  
        Args:
            emb_dims: embedding dimensions
            no_of_cont: number of continuous features
            lin_layer_sizes: sizes of linear layers
            emb_dropout: dropout for the embedding layer
            lin_layer_dropouts: dropouts for linear layers            
            in_channels: number of input channels
            out_channels: number of output channels after first covolution layer
            kernel_size: kernel size of first covolution layer
            distal_radius: distal radius of a focal site to be considered
            distal_order: sequece order for distal sequences
            distal_fc_dropout: dropout for distal fc layer
            n_class: number of classes (labels)
            emb_padding_idx: number to be used for padding in embeddings
        """
        
        super(Network1, self).__init__()
        
        self.n_class = n_class
        
        self.kernel_size = kernel_size
        self.seq_len = distal_radius*2+1 - (distal_order-1)
        

        rb1_kernel_size = 3
        rb2_kernel_size = 3
        
        # 1st conv layer
        self.conv1 = nn.Sequential(
            nn.BatchNorm1d(in_channels), # This is important!
            nn.Conv1d(in_channels, out_channels, kernel_size, 1, (kernel_size-1)//2), # in_channels, out_channels, kernel_size
        )
        
        
        self.maxpool1 = nn.MaxPool1d(3, 3, 1)
        # 1st set of residual blocks
        self.RBs1 = nn.Sequential(*[ResBlock(out_channels, kernel_size=rb1_kernel_size, stride=1, padding=(rb1_kernel_size-1)//2, dilation=1) for x in range(2)])
            

        self.maxpool2 = nn.MaxPool1d(3, 3, 1)# kernel_size, stride  
        self.conv2 = nn.Sequential(    
            nn.BatchNorm1d(out_channels),
            nn.Conv1d(out_channels, out_channels, kernel_size, 1, (kernel_size-1)//2),
            #nn.ReLU(),
        )
        
        # 2nd set of residual blocks
        self.RBs2 = nn.Sequential(*[ResBlock(out_channels, kernel_size=rb2_kernel_size, stride=1, padding=(rb2_kernel_size-1)//2, dilation=1) for x in range(2)])

        self.maxpool3 = nn.MaxPool1d(3, 3, 1)
        self.conv3 = nn.Sequential(
            nn.BatchNorm1d(out_channels),
            nn.Conv1d(out_channels, out_channels, kernel_size, 1, (kernel_size-1)//2),
            nn.ReLU(),
        )
        
        cnn_fc_in_size = out_channels


        # Separate FC layers for distal and local data
        self.distal_fc1 = nn.Sequential(
            nn.BatchNorm1d(cnn_fc_in_size),
            nn.Dropout(distal_fc_dropout), 
            nn.Linear(cnn_fc_in_size, n_class), 
            #nn.ReLU(),
            
        )

        # 1st conv layer
        self.conv1_2 = nn.Sequential(
            nn.BatchNorm1d(in_channels), # This is important!
            nn.Conv1d(in_channels, out_channels, kernel_size, 1, (kernel_size-1)//2), # in_channels, out_channels, kernel_size

        )
        
        
        self.maxpool1_2 = nn.MaxPool1d(15, 15, 7)
        # 1st set of residual blocks
        self.RBs1_2 = nn.Sequential(*[ResBlock(out_channels, kernel_size=rb1_kernel_size, stride=1, padding=(rb1_kernel_size-1)//2, dilation=1) for x in range(2)])
            

        self.maxpool2_2 = nn.MaxPool1d(7, 7, 3)# kernel_size, stride  
        self.conv2_2 = nn.Sequential(    
            nn.BatchNorm1d(out_channels),
            nn.Conv1d(out_channels, out_channels, kernel_size, 1, (kernel_size-1)//2),
            #nn.ReLU(),
        )
        
        # 2nd set of residual blocks
        self.RBs2_2 = nn.Sequential(*[ResBlock(out_channels, kernel_size=rb2_kernel_size, stride=1, padding=(rb2_kernel_size-1)//2, dilation=1) for x in range(2)])

        self.maxpool3_2 = nn.MaxPool1d(3, 3, 1)
        self.conv3_2 = nn.Sequential(
            nn.BatchNorm1d(out_channels),
            nn.Conv1d(out_channels, out_channels, kernel_size, 1, (kernel_size-1)//2),
            nn.ReLU(),
        )
        
        cnn_fc_in_size = out_channels

        # Separate FC layers for distal and local data
        self.distal_fc2 = nn.Sequential(
            nn.BatchNorm1d(cnn_fc_in_size),
            nn.Dropout(distal_fc_dropout), 
            nn.Linear(cnn_fc_in_size, n_class), 
            #nn.ReLU(),
            
        )
           
    
    def forward(self, local_input, distal_input):
        """
        Forward pass
        
        Args:
            local_input: local input
            distal_input: distal input
        """
        
        # CNN layers for distal_input
        # Input data shape: batch_size, in_channels, L_in (lenth of sequence)
        assert distal_input.shape[2] > 200, "Error: distal seq len must be >200bp"
        
        distal_input0 = distal_input[:,:,(distal_input.shape[2]//2-100):(distal_input.shape[2]//2+100+1)].detach().clone()
        distal_out = self.conv1(distal_input0) #output shape: batch_size, L_out; L_out = floor((L_in+2*padding-kernel_size)/stride + 1) 
        jump_input = distal_out = self.maxpool1(distal_out)
        
        distal_out = self.RBs1(distal_out)    
        assert(jump_input.shape[2] >= distal_out.shape[2])
        distal_out = distal_out + jump_input[:,:,0:distal_out.shape[2]]    
        distal_out = self.maxpool2(distal_out)
        
        jump_input = distal_out = self.conv2(distal_out)    
        distal_out = self.RBs2(distal_out)
        assert(jump_input.shape[2] >= distal_out.shape[2])
        distal_out = distal_out + jump_input[:,:,0:distal_out.shape[2]]
        distal_out = self.maxpool3(distal_out)
        
        distal_out = self.conv3(distal_out)
        distal_out, _ = torch.max(distal_out, dim=2)
        

        distal_out = self.distal_fc1(distal_out)
##############################
        # Input data shape: batch_size, in_channels, L_in (lenth of sequence)
        distal_out2 = self.conv1_2(distal_input) #output shape: batch_size, L_out; L_out = floor((L_in+2*padding-kernel_size)/stride + 1) 
        jump_input2 = distal_out2 = self.maxpool1_2(distal_out2)
        
        distal_out2 = self.RBs1_2(distal_out2)    
        assert(jump_input2.shape[2] >= distal_out2.shape[2])
        distal_out2 = distal_out2 + jump_input2[:,:,0:distal_out2.shape[2]]    
        distal_out2 = self.maxpool2_2(distal_out2)
        
        jump_input2 = distal_out2 = self.conv2_2(distal_out2)    
        distal_out2 = self.RBs2_2(distal_out2)
        assert(jump_input2.shape[2] >= distal_out2.shape[2])
        distal_out2 = distal_out2 + jump_input2[:,:,0:distal_out2.shape[2]]
        distal_out2 = self.maxpool3_2(distal_out2)
        
        distal_out2 = self.conv3_2(distal_out2)
        distal_out2, _ = torch.max(distal_out2, dim=2)
        

        distal_out2 = self.distal_fc2(distal_out2)

##############################
        
        #distal_out = torch.log((F.softmax(mid_out1, dim=1) +F.softmax(mid_out2, dim=1) + F.softmax(distal_out, dim=1))/3)
        distal_out = torch.log(torch.clamp((F.softmax(distal_out, dim=1)+ F.softmax(distal_out2, dim=1))/2, min=1e-9))
         
        
        return distal_out
    
    
class Network2(nn.Module):
    """Combined model with FeedForward and ResNet componets"""
    def __init__(self,  emb_dims, no_of_cont, lin_layer_sizes, emb_dropout, lin_layer_dropouts, in_channels, out_channels, kernel_size, distal_radius, distal_order, distal_fc_dropout, n_class, emb_padding_idx=None):
        """  
        Args:
            emb_dims: embedding dimensions
            no_of_cont: number of continuous features
            lin_layer_sizes: sizes of linear layers
            emb_dropout: dropout for the embedding layer
            lin_layer_dropouts: dropouts for linear layers            
            in_channels: number of input channels
            out_channels: number of output channels after first covolution layer
            kernel_size: kernel size of first covolution layer
            distal_radius: distal radius of a focal site to be considered
            distal_order: sequece order for distal sequences
            distal_fc_dropout: dropout for distal fc layer
            n_class: number of classes (labels)
            emb_padding_idx: number to be used for padding in embeddings
        """
        
        super(Network2, self).__init__()
        
        self.n_class = n_class
        
        # FeedForward layers for local input
        # Embedding layers
        print('emb_dims: ', emb_dims)
        print('emb_padding_idx: ', emb_padding_idx)
        self.no_of_cat = len(emb_dims)
        
        #self.emb_layers = nn.ModuleList([nn.Embedding(emb_padding_idx+1, y, padding_idx = emb_padding_idx) for x, y in emb_dims])
        self.emb_layer = nn.Embedding(emb_padding_idx+1, 5)

        #no_of_embs = sum([y for x, y in emb_dims])
        self.no_of_embs = len(emb_dims)*5
        self.no_of_cont = no_of_cont

        # Linear Layers
        first_lin_layer = nn.Linear(self.no_of_embs + self.no_of_cont, lin_layer_sizes[0])

        self.lin_layers = nn.ModuleList([first_lin_layer] + [nn.Linear(lin_layer_sizes[i], lin_layer_sizes[i + 1]) for i in range(len(lin_layer_sizes) - 1)])

        # Batch Norm Layers
        self.first_bn_layer = nn.BatchNorm1d(self.no_of_cont)
        self.bn_layers = nn.ModuleList([nn.BatchNorm1d(size) for size in lin_layer_sizes])

        # Dropout Layers
        self.emb_dropout_layer = nn.Dropout(emb_dropout)
        self.droput_layers = nn.ModuleList([nn.Dropout(size) for size in lin_layer_dropouts])
        
        self.kernel_size = kernel_size
        self.seq_len = distal_radius*2+1 - (distal_order-1)
        

        rb1_kernel_size = 3
        rb2_kernel_size = 3
        
        ## for middle-scale sequence space
        # 1st conv layer
        self.conv1 = nn.Sequential(
            nn.BatchNorm1d(in_channels), # This is important!
            nn.Conv1d(in_channels, out_channels, kernel_size, 1, (kernel_size-1)//2), # in_channels, out_channels, kernel_size
        )
        
        
        self.maxpool1 = nn.MaxPool1d(3, 3, 1)
        # 1st set of residual blocks
        self.RBs1 = nn.Sequential(*[ResBlock(out_channels, kernel_size=rb1_kernel_size, stride=1, padding=(rb1_kernel_size-1)//2, dilation=1) for x in range(2)])
            

        self.maxpool2 = nn.MaxPool1d(3, 3, 1)# kernel_size, stride  
        self.conv2 = nn.Sequential(    
            nn.BatchNorm1d(out_channels),
            nn.Conv1d(out_channels, out_channels, kernel_size, 1, (kernel_size-1)//2),
            #nn.ReLU(),
        )
        
        # 2nd set of residual blocks
        self.RBs2 = nn.Sequential(*[ResBlock(out_channels, kernel_size=rb2_kernel_size, stride=1, padding=(rb2_kernel_size-1)//2, dilation=1) for x in range(2)])

        self.maxpool3 = nn.MaxPool1d(3, 3, 1)
        self.conv3 = nn.Sequential(
            nn.BatchNorm1d(out_channels),
            nn.Conv1d(out_channels, out_channels, kernel_size, 1, (kernel_size-1)//2),
            nn.ReLU(),
        )
        
        cnn_fc_in_size = out_channels


        # Separate FC layers for distal and local data
        self.distal_fc1 = nn.Sequential(
            nn.BatchNorm1d(cnn_fc_in_size),
            nn.Dropout(distal_fc_dropout), 
            nn.Linear(cnn_fc_in_size, n_class), 
            #nn.ReLU(),
            
        )

        ## for large-scale sequence space
        # 1st conv layer
        self.conv1_2 = nn.Sequential(
            nn.BatchNorm1d(in_channels), # This is important!
            nn.Conv1d(in_channels, out_channels, kernel_size, 1, (kernel_size-1)//2), # in_channels, out_channels, kernel_size

        )
        
        
        self.maxpool1_2 = nn.MaxPool1d(15, 15, 7)
        # 1st set of residual blocks
        self.RBs1_2 = nn.Sequential(*[ResBlock(out_channels, kernel_size=rb1_kernel_size, stride=1, padding=(rb1_kernel_size-1)//2, dilation=1) for x in range(2)])
            

        self.maxpool2_2 = nn.MaxPool1d(7, 7, 3)# kernel_size, stride  
        self.conv2_2 = nn.Sequential(    
            nn.BatchNorm1d(out_channels),
            nn.Conv1d(out_channels, out_channels, kernel_size, 1, (kernel_size-1)//2),
            #nn.ReLU(),
        )
        
        # 2nd set of residual blocks
        self.RBs2_2 = nn.Sequential(*[ResBlock(out_channels, kernel_size=rb2_kernel_size, stride=1, padding=(rb2_kernel_size-1)//2, dilation=1) for x in range(2)])

        self.maxpool3_2 = nn.MaxPool1d(3, 3, 1)
        self.conv3_2 = nn.Sequential(
            nn.BatchNorm1d(out_channels),
            nn.Conv1d(out_channels, out_channels, kernel_size, 1, (kernel_size-1)//2),
            nn.ReLU(),
        )
        
        cnn_fc_in_size = out_channels

        # Separate FC layers for distal and local data
        self.distal_fc2 = nn.Sequential(
            nn.BatchNorm1d(cnn_fc_in_size),
            nn.Dropout(distal_fc_dropout), 
            nn.Linear(cnn_fc_in_size, n_class), 
            #nn.ReLU(),
            
        )
        
        # Local FC layers
        self.local_fc = nn.Sequential(
            #nn.BatchNorm1d(lin_layer_sizes[-1]),
            #nn.Dropout(0.15),
            nn.Linear(lin_layer_sizes[-1], n_class), 
        )       
    
    def forward(self, local_input, distal_input):
        """
        Forward pass
        
        Args:
            local_input: local input
            distal_input: distal input
        """
        
        # FeedForward layers for local input
        cont_data, cat_data = local_input
        
        if self.no_of_embs != 0:
            local_out = [self.emb_layer(cat_data[:, i]) for i in range(self.no_of_cat)]
            
        local_out = torch.cat(local_out, dim = 1) #x.shape: batch_size * sum(emb_size)
        local_out = self.emb_dropout_layer(local_out)

        if self.no_of_cont != 0:
            normalized_cont_data = self.first_bn_layer(cont_data)

            if self.no_of_embs != 0:
                local_out = torch.cat([local_out, normalized_cont_data], dim = 1) 
            else:
                local_out = normalized_cont_data
        
        for lin_layer, dropout_layer, bn_layer in zip(self.lin_layers, self.droput_layers, self.bn_layers):
            local_out = F.relu(lin_layer(local_out))
            local_out = bn_layer(local_out)
            local_out = dropout_layer(local_out)
        
        assert distal_input.shape[2] > 200, "Error: distal seq len must be >200"
        # CNN layers for distal_input
        # Input data shape: batch_size, in_channels, L_in (lenth of sequence)
        distal_input0 = distal_input[:,:,(distal_input.shape[2]//2-100):(distal_input.shape[2]//2+100+1)].detach().clone()
        distal_out = self.conv1(distal_input0) #output shape: batch_size, L_out; L_out = floor((L_in+2*padding-kernel_size)/stride + 1) 
        jump_input = distal_out = self.maxpool1(distal_out)
        
        distal_out = self.RBs1(distal_out)    
        assert(jump_input.shape[2] >= distal_out.shape[2])
        distal_out = distal_out + jump_input[:,:,0:distal_out.shape[2]]    
        distal_out = self.maxpool2(distal_out)
        
        jump_input = distal_out = self.conv2(distal_out)    
        distal_out = self.RBs2(distal_out)
        assert(jump_input.shape[2] >= distal_out.shape[2])
        distal_out = distal_out + jump_input[:,:,0:distal_out.shape[2]]
        distal_out = self.maxpool3(distal_out)
        
        distal_out = self.conv3(distal_out)
        distal_out, _ = torch.max(distal_out, dim=2)
        
        # Separate FC layers 
        local_out = self.local_fc(local_out)
        distal_out = self.distal_fc1(distal_out)
##############################
        # Input data shape: batch_size, in_channels, L_in (lenth of sequence)
        distal_out2 = self.conv1_2(distal_input) #output shape: batch_size, L_out; L_out = floor((L_in+2*padding-kernel_size)/stride + 1) 
        jump_input2 = distal_out2 = self.maxpool1_2(distal_out2)
        
        distal_out2 = self.RBs1_2(distal_out2)    
        assert(jump_input2.shape[2] >= distal_out2.shape[2])
        distal_out2 = distal_out2 + jump_input2[:,:,0:distal_out2.shape[2]]    
        distal_out2 = self.maxpool2_2(distal_out2)
        
        jump_input2 = distal_out2 = self.conv2_2(distal_out2)    
        distal_out2 = self.RBs2_2(distal_out2)
        assert(jump_input2.shape[2] >= distal_out2.shape[2])
        distal_out2 = distal_out2 + jump_input2[:,:,0:distal_out2.shape[2]]
        distal_out2 = self.maxpool3_2(distal_out2)
        
        distal_out2 = self.conv3_2(distal_out2)
        distal_out2, _ = torch.max(distal_out2, dim=2)
        

        distal_out2 = self.distal_fc2(distal_out2)

##############################
        
        #distal_out = torch.log((F.softmax(mid_out1, dim=1) +F.softmax(mid_out2, dim=1) + F.softmax(distal_out, dim=1))/3)
        #distal_out = torch.log((F.softmax(distal_out, dim=1)+ F.softmax(distal_out2, dim=1))/2)
        distal_out = (F.softmax(distal_out, dim=1)+ F.softmax(distal_out2, dim=1))/2
        local_out = F.softmax(local_out, dim=1)
        
        if self.training == False and np.random.uniform(0,1) < 0.00001*local_out.shape[0]:
            print('local_out1:', torch.min(local_out[:,1]).item(), torch.max(local_out[:,1]).item(), torch.var(local_out[:,1]).item())
            print('distal_out1:', torch.min(distal_out[:,1]).item(), torch.max(distal_out[:,1]).item(),torch.var(distal_out[:,1]).item())

        
        out = torch.log(torch.clamp((local_out + distal_out)/2, min=1e-9))  
        
        return out
    
# Residual block (according to Jaganathan et al. 2019 Cell)
class ResBlock(nn.Module):
    """Residual block unit"""
    def __init__(self, in_channels=32, kernel_size=3, stride=1, padding=0, dilation=1):
        super(ResBlock, self).__init__()
        
        self.bn1 = nn.BatchNorm1d(in_channels)
        self.conv1 = nn.Conv1d(in_channels, in_channels, kernel_size=kernel_size, stride=stride, padding=padding, dilation=dilation)  
        self.bn2 = nn.BatchNorm1d(in_channels)
        self.conv2 = nn.Conv1d(in_channels, in_channels, kernel_size=kernel_size, stride=stride, padding=padding, dilation=dilation)        
        
        self.layer = nn.Sequential(nn.ReLU(),self.bn1, self.conv1, nn.ReLU(), self.bn2, self.conv2)

    def forward(self, x):
        out = self.layer(x)
        #print('out.shape, x.shape:', out.shape, x.shape)
        d = x.shape[2] - out.shape[2]
        out = x[:,:,0:x.shape[2]-d] + out
        
        return out

class ResBlock2(nn.Module):
    """Residual block unit"""
    def __init__(self, in_channels=32, kernel_size=3, stride=1, padding=0, dilation=1):
        super(ResBlock2, self).__init__()
        
        self.bn1 = nn.BatchNorm1d(in_channels)
        self.conv1 = nn.Conv1d(in_channels, in_channels, kernel_size=kernel_size, stride=stride, padding=padding, dilation=dilation)  
        self.bn2 = nn.BatchNorm1d(in_channels)
        self.conv2 = nn.Conv1d(in_channels, in_channels, kernel_size=kernel_size, stride=stride, padding=padding, dilation=dilation)        
        
        self.layer = nn.Sequential(self.bn1, nn.ReLU(),self.conv1, self.bn2, nn.ReLU(), self.conv2)

    def forward(self, x):
        out = self.layer(x)
        #print('out.shape, x.shape:', out.shape, x.shape)
        d = x.shape[2] - out.shape[2]
        out = x[:,:,0:x.shape[2]-d] + out
        
        return out
    
# Residual block ('bottleneck' version)
class ResidualBlock(nn.Module):
    def __init__(self, in_channels, out_channels, stride=1):
        """Residual block ('bottleneck' version)"""
        super(ResidualBlock, self).__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.stride = stride
        self.bn1 = nn.BatchNorm1d(in_channels)
        self.relu = nn.ReLU(inplace=True)
        self.conv1 = nn.Conv1d(in_channels, out_channels//4, 1, 1, bias = False)
        self.bn2 = nn.BatchNorm1d(out_channels//4)
        self.relu = nn.ReLU(inplace=True)
        self.conv2 = nn.Conv1d(out_channels//4, out_channels//4, 3, stride, padding = 1, bias = False)
        #new_seq_len = (seq_len + 2*padding - kernel_size + stride)//stride
        #seq_len1 = (self.seq_len + 2 * 1 - (3 - stride))//stride
        self.bn3 = nn.BatchNorm1d(out_channels//4)
        self.relu = nn.ReLU(inplace=True)
        self.conv3 = nn.Conv1d(out_channels//4, out_channels, 1, 1, bias = False)
        self.conv4 = nn.Conv1d(in_channels, out_channels, 1, stride, bias = False)
        #new_seq_len = (seq_len + 2*padding - kernel_size + stride)//stride
        #seq_len1 = (self.seq_len + 2 * 0 - (1 - stride))//stride
        
    def forward(self, x):
        residual = x
        out = self.bn1(x)
        out1 = self.relu(out)
        out = self.conv1(out1)
        out = self.bn2(out)
        out = self.relu(out)
        out = self.conv2(out)
        out = self.bn3(out)
        out = self.relu(out)
        out = self.conv3(out)
        if (self.in_channels != self.out_channels) or (self.stride !=1 ):
            residual = self.conv4(out1)
        out += residual
        return out   


class MuTransformer(nn.Module):
    """ResNet-only model"""
    def __init__(self, in_channels, out_channels, kernel_size, distal_radius, distal_order, distal_fc_dropout, n_class, nhead, dim_feedforward, trans_dropout, num_layers):
        """  
        Args:
            in_channels: number of input channels
            out_channels: number of output channels after first covolution layer
            kernel_size: kernel size of first covolution layer
            distal_radius: distal radius of a focal site to be considered
            distal_order: sequece order for distal sequences
            n_class: number of classes (labels)
        """
        super(MuTransformer, self).__init__()
        
        print("Using Transformer ...")
        
        self.n_class = n_class     
        
        self.kernel_size = kernel_size
        self.seq_len = distal_radius * 2 + 1 - (distal_order - 1)
        
        # 1st conv layer
        self.conv1 = nn.Sequential(
            nn.BatchNorm1d(in_channels), # This is important!
            nn.Conv1d(in_channels, out_channels, kernel_size, stride=1, padding=(kernel_size-1)//2), # in_channels, out_channels, kernel_size
            #nn.ReLU(),
        )
        
        self.pos_encoder = PositionalEncoding(
            d_model=out_channels,
            dropout=trans_dropout,
            max_len=self.seq_len,
        )
        
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=out_channels,
            nhead=nhead,
            dim_feedforward=dim_feedforward,
            dropout=trans_dropout,
            activation='gelu',
        )
        self.transformer_encoder = nn.TransformerEncoder(
            encoder_layer,
            num_layers=num_layers,
        ) 
        
        # Separate FC layers for distal and local data
        self.classifier = nn.Sequential(
            nn.BatchNorm1d(out_channels),
            nn.Dropout(distal_fc_dropout), 
            nn.Linear(out_channels, n_class), 
            #nn.ReLU(),
            
        )
        
        self.d_model = out_channels
        
    
    def forward(self, local_input, distal_input):
        """
        Forward pass
        
        Args:
            local_input: local input
            distal_input: distal input
        """
        
        x = self.conv1(distal_input) #output shape: batch_size, out_channels, L_out
        x = x.permute(2, 0, 1)
        x = x * math.sqrt(self.d_model)
        x = self.pos_encoder(x)
        x = self.transformer_encoder(x)
        x = x.mean(dim=0)
        x = self.classifier(x)
        
        return x
        
'''
class PositionalEncoding(nn.Module):
    """
    https://pytorch.org/tutorials/beginner/transformer_tutorial.html
    """
    def __init__(self, d_model, vocab_size=5000, dropout=0.1):
        super().__init__()
        self.dropout = nn.Dropout(p=dropout)
        pe = torch.zeros(vocab_size, d_model)
        position = torch.arange(0, vocab_size, dtype=torch.float).unsqueeze(1)
        div_term = torch.exp(
            torch.arange(0, d_model, 2).float()
            * (-math.log(10000.0) / d_model)
        )
        pe[:, 0::2] = torch.sin(position * div_term)
        pe[:, 1::2] = torch.cos(position * div_term)
        pe = pe.unsqueeze(0)
        self.register_buffer("pe", pe)
    def forward(self, x):
        print("x.shape, pe.shape", x.shape, self.pe.shape)
        x = x + self.pe[:, : x.size(1), :]
        return self.dropout(x)
'''

class PositionalEncoding(nn.Module):

    def __init__(self, d_model: int, dropout: float = 0.1, max_len: int = 5000):
        super().__init__()
        self.dropout = nn.Dropout(p=dropout)

        position = torch.arange(max_len).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2) * (-math.log(10000.0) / d_model))
        pe = torch.zeros(max_len, 1, d_model)
        pe[:, 0, 0::2] = torch.sin(position * div_term)
        pe[:, 0, 1::2] = torch.cos(position * div_term)
        self.register_buffer('pe', pe)

    def forward(self, x):
        """
        Args:
            x: Tensor, shape [seq_len, batch_size, embedding_dim]
        """
        x = x + self.pe[:x.size(0)]
        return self.dropout(x)