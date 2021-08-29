"""
Code for training models with RayTune
"""

import warnings
warnings.filterwarnings('ignore',category=FutureWarning)

from pybedtools import BedTool

import sys
import argparse
import pandas as pd
import numpy as np
import pickle

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader
from torch.utils.data import random_split

torch.backends.cuda.matmul.allow_tf32 = True
torch.backends.cudnn.benchmark = True
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.allow_tf32 = True

from functools import partial
import ray
from ray import tune
from ray.tune import CLIReporter
from ray.tune.schedulers import ASHAScheduler

import os
import time
import datetime
import random

from MuRaL.nn_models import *
from MuRaL.nn_utils import *
from MuRaL.preprocessing import *
from MuRaL.evaluation import *
from MuRaL.training import *

import textwrap
#from torch.utils.tensorboard import SummaryWriter

def parse_arguments(parser):
    """
    Parse parameters from the command line
    """   
    optional = parser._action_groups.pop()
    required = parser.add_argument_group('required arguments')
    
    required.add_argument('--ref_genome', type=str, metavar='FILE', default='',  
                          required=True, help=textwrap.dedent("""
                          File path of the reference genome in FASTA format.""").strip())
    
    required.add_argument('--train_data', type=str, metavar='FILE', default='',  
                          required=True, help= textwrap.dedent("""
                          File path of training data in a sorted BED format. If the options
                          --validation_data and --valid_ratio not specified, 10%% of the
                          sites sampled from the training BED will be used as the
                          validation data.""").strip())
    
    optional.add_argument('--validation_data', type=str, metavar='FILE', default=None,
                          help=textwrap.dedent("""
                          File path for validation data. If this option is set,
                          the value of --valid_ratio will be ignored.""").strip()) 
    
    optional.add_argument('--bw_paths', type=str, metavar='FILE', default=None,
                          help=textwrap.dedent("""
                          File path for a list of BigWig files for non-sequence 
                          features such as the coverage track. Default: None.""").strip())
    
    optional.add_argument('--seq_only', default=False, action='store_true', 
                          help=textwrap.dedent("""
                          If set, use only genomic sequences for the model and ignore
                          bigWig tracks. Default: False.""").strip())
    
    optional.add_argument('--n_class', type=int, metavar='INT', default='4',  
                          help=textwrap.dedent("""
                          Number of mutation classes (or types), including the 
                          non-mutated class. Default: 4.""").strip())
    
    optional.add_argument('--local_radius', type=int, metavar='INT', default=[5], nargs='+',
                          help=textwrap.dedent("""
                          Radius of the local sequence to be considered in the 
                          model. Length of the local sequence = local_radius*2+1 bp.
                          If multiple space-separated values are provided, one value
                          will be randomly chosen for each trial.""" ).strip())
    
    optional.add_argument('--local_order', type=int, metavar='INT', default=[1], nargs='+', 
                          help=textwrap.dedent("""
                          Length of k-mer in the embedding layer.""").strip())
    
    optional.add_argument('--local_hidden1_size', type=int, metavar='INT', default=[150], nargs='+', 
                          help=textwrap.dedent("""
                          Size of 1st hidden layer for local module.""").strip())
    
    optional.add_argument('--local_hidden2_size', type=int, metavar='INT', default=[0], nargs='+',
                          help=textwrap.dedent("""
                          Size of 2nd hidden layer for local module.""" ).strip())
    
    optional.add_argument('--distal_radius', type=int, metavar='INT', default=[50], nargs='+', 
                          help=textwrap.dedent("""
                          Radius of the expanded sequence to be considered in the model. 
                          Length of the expanded sequence = distal_radius*2+1 bp.
                          """ ).strip())
    
    optional.add_argument('--distal_order', type=int, metavar='INT', default=1, 
                          help=textwrap.dedent("""
                          Order of distal sequences to be considered. Kept for 
                          future development.""" ).strip())
        
    optional.add_argument('--batch_size', type=int, metavar='INT', default=[128], nargs='+', 
                          help=textwrap.dedent("""
                          Size of mini batches for training.
                          """ ).strip())
    
    optional.add_argument('--emb_dropout', type=float, metavar='FLOAT', default=[0.1], nargs='+', 
                          help=textwrap.dedent("""
                          Dropout rate for inputs of the k-mer embedding layer""" ).strip())
    
    optional.add_argument('--local_dropout', type=float, metavar='FLOAT', default=[0.1], nargs='+', 
                          help=textwrap.dedent("""
                          Dropout rate for inputs of local hidden layers.""" ).strip())
    
    optional.add_argument('--CNN_kernel_size', type=int, metavar='INT', default=[3], nargs='+', 
                          help=textwrap.dedent("""
                          Kernel size for CNN layers in the expanded module.""" ).strip())
    
    optional.add_argument('--CNN_out_channels', type=int, metavar='INT', default=[32], nargs='+', 
                          help=textwrap.dedent("""
                          Number of output channels for CNN layers.""" ).strip())
    
    optional.add_argument('--distal_fc_dropout', type=float, metavar='FLOAT', default=[0.25], nargs='+', 
                          help=textwrap.dedent("""
                          Dropout rate for the FC layer of the expanded module.""" ).strip())
    
    
    optional.add_argument('--model_no', type=int, metavar='INT', default=2, 
                          help=textwrap.dedent("""
                          Which network architecture to be used: 
                          0, local-only model;
                          1, expanded-only model;
                          2, local + expanded model.""" ).strip())
    
                          
    optional.add_argument('--optim', type=str, metavar='STRING', default=['Adam'], nargs='+', 
                          help=textwrap.dedent("""
                          Name of optimization method for learning.
                          """ ).strip())
    
    optional.add_argument('--cuda_id', type=str, metavar='STRING', default='0', 
                          help=textwrap.dedent("""
                          Which GPU device to be used.""" ).strip())
    
    optional.add_argument('--valid_ratio', type=float, metavar='FLOAT', default=0.1, 
                          help=textwrap.dedent("""
                          Ratio of validation data relative to the whole training data.
                          """ ).strip())
    
    optional.add_argument('--split_seed', type=int, metavar='INT', default=-1, 
                          help=textwrap.dedent("""
                          Seed for randomly splitting data into training and validation sets.
                          """ ).strip())
    
    optional.add_argument('--learning_rate', type=float, metavar='FLOAT', default=[0.005], nargs='+', 
                          help=textwrap.dedent("""
                          Learning rate for network training, a parameter for the optimization
                          method.""" ).strip())
    
    optional.add_argument('--weight_decay', type=float, metavar='FLOAT', default=[1e-5], nargs='+', 
                          help=textwrap.dedent("""
                          'weight_decay' parameter (regularization) for the optimization 
                          method.""" ).strip())
    
    optional.add_argument('--LR_gamma', type=float, metavar='FLOAT', default=[0.5], nargs='+', 
                          help=textwrap.dedent("""
                          'gamma' parameter for the learning rate scheduler.""" ).strip())
    
    optional.add_argument('--epochs', type=int, metavar='INT', default=10, 
                          help=textwrap.dedent("""
                          Maximum number of epochs for each trial.""" ).strip())
    
    optional.add_argument('--grace_period', type=int, metavar='INT', default=5, 
                          help=textwrap.dedent("""
                          'grace_period' parameter for early stopping.""" ).strip())
    
    
    optional.add_argument('--n_trials', type=int, metavar='INT', default=3, 
                          help=textwrap.dedent("""
                          Number of trials for this training job.""" ).strip())
    
    optional.add_argument('--experiment_name', type=str, metavar='STRING', default='my_experiment',
                          help=textwrap.dedent("""
                          Ray-Tune experiment name.""" ).strip())
    
    optional.add_argument('--ASHA_metric', type=str, metavar='STRING', default='loss', 
                          help=textwrap.dedent("""
                          Metric for ASHA schedualing; the value can be 'loss' or 'score'.""" ).strip())
    
    optional.add_argument('--ray_ncpus', type=int, metavar='INT', default=6, 
                          help=textwrap.dedent("""
                          Number of CPUs requested by Ray-Tune. """ ).strip())
    
    optional.add_argument('--ray_ngpus', type=int, metavar='INT', default=1, 
                          help=textwrap.dedent("""
                          Number of GPUs requested by Ray-Tune.""" ).strip())
    
    optional.add_argument('--cpu_per_trial', type=int, metavar='INT', default=3, 
                          help=textwrap.dedent("""
                          Number of CPUs used per trial.""" ).strip())
    
    optional.add_argument('--gpu_per_trial', type=float, metavar='FLOAT', default=0.19, 
                          help=textwrap.dedent("""
                          Number of GPUs used per trial""" ).strip())
        
    optional.add_argument('--save_valid_preds', default=False, action='store_true', 
                          help=textwrap.dedent("""
                          Save prediction results for validation data in the checkpoint folders.""" ).strip())
    
    optional.add_argument('--rerun_failed', default=False, action='store_true', 
                          help=textwrap.dedent("""
                          Rerun failed trials""" ).strip())
    
    parser._action_groups.append(optional)
    
    if len(sys.argv) == 1:
        parser.parse_args(['--help'])
    else:
        args = parser.parse_args()

    return args
def main():
    
    #parse the command line
    #parser = argparse.ArgumentParser(formatter_class=argparse.RawTextHelpFormatter,
    #parser = argparse.ArgumentParser(formatter_class=argparse.RawDescriptionHelpFormatter,
    parser = argparse.ArgumentParser(formatter_class=argparse.RawTextHelpFormatter,
                                     description="""
    Overview
    --------
    
    This tool trains MuRaL models with training and validation mutation data
    and exports training results under the "./ray_results/" folder.
    
    * Input data
    MuRaL requires input training and validation data files to be in BED format
    (more info about BED at https://genome.ucsc.edu/FAQ/FAQformat.html#format1). 
    Some example lines of the input BED file are shown below.
    chr1	2333436	2333437	.	0	+
    chr1	2333446	2333447	.	2	-
    chr1	2333468	2333469	.	1	-
    chr1	2333510	2333511	.	3	-
    chr1	2333812	2333813	.	0	-
    
    In the BED-formatted lines above, the 5th column is used to represent mutation
    status: usually, '0' means the non-mutated status and other numbers means 
    specific mutation types (e.g. '1' for A>C, '2' for A>G, '3' for 'A>T'). You can
    specify a arbitrary order for a group of mutation types with incremental 
    numbers starting from 1, but make sure that the same order is consistently 
    used in training, validation and testing datasets. 
    
    Importantly, the training and validation BED file MUST be SORTED by chromosome
    coordinates. You can sort BED files by 'bedtools sort' or 'sort -k1,1 -k2,2n'.
    
    * Output data
    The checkpointed model files during training are saved under folders named like 
        ./ray_results/your_experiment_name/Train_xxx...xxx/checkpoint_x/
            - model
            - model.config.pkl
            - model.fdiri_cal.pkl
    
    In the above folder, the 'model' file contains the learned model parameters. 
    The 'model.config.pkl' file contains configured hyperparameters of the model.
    The 'model.fdiri_cal.pkl' file (if exists) contains the calibration model 
    learned with validation data, which can be used for calibrating predicted 
    mutation rates. These files will be used in downstream analyses such as
    model prediction and transfer learning.
    
    Command line examples
    ---------------------
    
    1. The following command will train a model by running two trials, using data in
    'train.sorted.bed' for training. The training results will be saved under the
    folder './ray_results/example1/'. Default values will be used for other
    unspecified arguments. Note that, by default, 10% of the sites sampled from 
    'train.sorted.bed' is used as validation data (i.e., '--valid_ratio 0.1').
    
        mural_train --ref_genome seq.fa --train_data train.sorted.bed \\
        --n_trials 2 --experiment_name example1 > test1.out 2> test1.err
    
    2. The following command will use data in 'train.sorted.bed' as training
    data and a separate 'validation.sorted.bed' as validation data. The option
    '--local_radius 10' means that length of the local sequence used for training
    is 10*2+1 = 21 bp. '--distal_radius 100' means that length of the expanded 
    sequence used for training is 100*2+1 = 201 bp. 
    
        mural_train --ref_genome seq.fa --train_data train.sorted.bed \\
        --validation_data validation.sorted.bed --n_trials 2 --local_radius 10 \\ 
        --distal_radius 100 --experiment_name example2 > test2.out 2> test2.err
    
    3. If you don't have (or don't want to use) GPU resources, you can set options
    '--ray_ngpus 0 --gpu_per_trial 0' as below. Be aware that if training dataset 
    is large or the model is parameter-rich, CPU-only computing could take a very 
    long time!
    
        mural_train --ref_genome seq.fa --train_data train.sorted.bed \\
        --n_trials 2 --ray_ngpus 0 --gpu_per_trial 0 --experiment_name example3 \\ 
        > test3.out 2> test3.err
    
    Notes
    -----
    1. The training and validation BED file MUST be SORTED by chromosome 
    coordinates. You can sort BED files by running 'bedtools sort' or 
    'sort -k1,1 -k2,2n'.
    
    2. By default, this tool generates a HDF5 file for each input BED
    file (training or validation file) based on the value of '--distal_radius' 
    and the tracks in '--bw_paths' if the corresponding HDF5 file doesn't 
    exist or is corrupted. Only one job is allowed to write to an HDF5 file,
    so don't run multiple jobs involving a same BED file when its HDF5 file 
    isn't generated yet. Otherwise, it may cause file permission errors.
    
    3. If it takes long to finish the job, you can check the information exported 
    to stdout (or redirected file) for the progress during running.
    
    """)
    
    args = parse_arguments(parser)
    
    start_time = time.time()
    print('Start time:', datetime.datetime.now())
    
    print(' '.join(sys.argv)) # print the command line
    # Ray requires absolute paths
    train_file  = args.train_data = os.path.abspath(args.train_data) 
    valid_file = args.validation_data
    if valid_file: 
        args.validation_data = os.path.abspath(args.validation_data)     
    ref_genome = args.ref_genome =  os.path.abspath(args.ref_genome)
    local_radius = args.local_radius
    local_order = args.local_order
    local_hidden1_size = args.local_hidden1_size
    local_hidden2_size = args.local_hidden2_size
    distal_radius = args.distal_radius  
    distal_order = args.distal_order
    batch_size = args.batch_size 
    emb_dropout = args.emb_dropout
    local_dropout = args.local_dropout
    CNN_kernel_size = args.CNN_kernel_size   
    CNN_out_channels = args.CNN_out_channels
    distal_fc_dropout = args.distal_fc_dropout
    model_no = args.model_no   
    #pred_file = args.pred_file   
    optim = args.optim
    learning_rate = args.learning_rate   
    weight_decay = args.weight_decay  
    LR_gamma = args.LR_gamma  
    epochs = args.epochs
    grace_period = args.grace_period
    n_trials = args.n_trials
    experiment_name = args.experiment_name
    ASHA_metric = args.ASHA_metric
    n_class = args.n_class  
    cuda_id = args.cuda_id
    valid_ratio = args.valid_ratio
    save_valid_preds = args.save_valid_preds
    rerun_failed = args.rerun_failed
    ray_ncpus = args.ray_ncpus
    ray_ngpus = args.ray_ngpus
    cpu_per_trial = args.cpu_per_trial
    gpu_per_trial = args.gpu_per_trial
    
    if args.split_seed < 0:
        args.split_seed = random.randint(0, 1000000)
    print('args.split_seed:', args.split_seed)
    
    
    # Read bigWig file names
    bw_paths = args.bw_paths
    bw_files = []
    bw_names = []
    
    if bw_paths:
        try:
            bw_list = pd.read_table(bw_paths, sep='\s+', header=None, comment='#')
            bw_files = list(bw_list[0])
            bw_names = list(bw_list[1])
        except pd.errors.EmptyDataError:
            print('Warnings: no bigWig files provided in', bw_paths)
    else:
        print('NOTE: no bigWig files provided.')
    
    # Prepare min/max for the loguniform samplers if one value is provided
    if len(learning_rate) == 1:
        learning_rate = learning_rate*2
    if len(weight_decay) == 1:
        weight_decay = weight_decay*2
    
    # Read the train datapoints
    train_bed = BedTool(train_file)
    
    # Generate H5 files for storing distal regions before training, one file for each possible distal radius
    for d_radius in distal_radius:
        h5f_path = get_h5f_path(train_file, bw_names, d_radius, distal_order)
        generate_h5f(train_bed, h5f_path, ref_genome, d_radius, distal_order, bw_files, 1)
    
    if valid_file:
        valid_bed = BedTool(valid_file)
        for d_radius in distal_radius:
            valid_h5f_path = get_h5f_path(valid_file, bw_names, d_radius, distal_order)
            generate_h5f(valid_bed, valid_h5f_path, ref_genome, d_radius, distal_order, bw_files, 1)
    
    if ray_ngpus > 0 or gpu_per_trial > 0:
        if not torch.cuda.is_available():
            print('Error: You requested GPU computing, but CUDA is not available! If you want to run without GPU, please set "--ray_ngpus 0 --gpu_per_trial 0"', file=sys.stderr)
            sys.exit()
        # Set visible GPU(s)
        os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        os.environ["CUDA_VISIBLE_DEVICES"] = cuda_id
        print('Ray is using GPU device', 'cuda:'+cuda_id)
    else:
        print('Ray is using only CPUs ...')
    
    if rerun_failed:
        resume_flag = 'ERRORED_ONLY'
    else:
        resume_flag = False
    
    # Allocate CPU/GPU resources for this Ray job
    ray.init(num_cpus=ray_ncpus, num_gpus=ray_ngpus, dashboard_host="0.0.0.0")
    
    sys.stdout.flush()
    
    # Configure the search space for relavant hyperparameters
    config = {
        'local_radius': tune.choice(local_radius),
        'local_order': tune.choice(local_order),
        'local_hidden1_size': tune.choice(local_hidden1_size),
        #'local_hidden2_size': tune.choice(local_hidden2_size),
        'local_hidden2_size': tune.choice(local_hidden2_size) if local_hidden2_size[0]>0 else tune.sample_from(lambda spec: spec.config.local_hidden1_size//2), # default local_hidden2_size = local_hidden1_size//2
        'distal_radius': tune.choice(distal_radius),
        'emb_dropout': tune.choice(emb_dropout),
        'local_dropout': tune.choice(local_dropout),
        'CNN_kernel_size': tune.choice(CNN_kernel_size),
        'CNN_out_channels': tune.choice(CNN_out_channels),
        'distal_fc_dropout': tune.choice(distal_fc_dropout),
        'batch_size': tune.choice(batch_size),
        'learning_rate': tune.loguniform(learning_rate[0], learning_rate[1]),
        #'learning_rate': tune.choice(learning_rate),
        'optim': tune.choice(optim),
        'LR_gamma': tune.choice(LR_gamma),
        'weight_decay': tune.loguniform(weight_decay[0], weight_decay[1]),
        #'weight_decay': tune.choice(weight_decay),
        'transfer_learning': False,
    }
    

    # Set the scheduler for parallel training 
    scheduler = ASHAScheduler(
    #metric='loss',
    metric=ASHA_metric, # Use a metric for model selection
    mode='min',
    max_t=epochs,
    grace_period=grace_period,
    reduction_factor=2)
    
    # Information to be shown in the progress table
    reporter = CLIReporter(parameter_columns=['local_radius', 'local_order', 'local_hidden1_size', 'local_hidden2_size', 'distal_radius', 'emb_dropout', 'local_dropout', 'CNN_kernel_size', 'CNN_out_channels', 'distal_fc_dropout', 'optim', 'learning_rate', 'weight_decay', 'LR_gamma', ], metric_columns=['loss', 'fdiri_loss', 'after_min_loss',  'score', 'total_params', 'training_iteration'])
    
    trainable_id = 'Train'
    tune.register_trainable(trainable_id, partial(train, args=args))
    
    # Execute the training
    result = tune.run(
    trainable_id,
    name=experiment_name,
    resources_per_trial={'cpu': cpu_per_trial, 'gpu': gpu_per_trial},
    config=config,
    num_samples=n_trials,
    local_dir='./ray_results',
    scheduler=scheduler,
    stop={'after_min_loss':3},
    progress_reporter=reporter,
    resume=resume_flag)
    
    # Print the best trial at the ende
    #best_trial = result.get_best_trial('loss', 'min', 'last')
    #best_trial = result.get_best_trial('loss', 'min', 'last-5-avg')
    #print('Best trial config: {}'.format(best_trial.config))
    #print('Best trial final validation loss: {}'.format(best_trial.last_result['loss'])) 
    
    #best_checkpoint = result.get_best_checkpoint(best_trial, metric='loss', mode='min')
    #print('best_checkpoint:', best_checkpoint)
    
    # Shutdown Ray
    if ray.is_initialized():
        ray.shutdown() 

    print('Total time used: %s seconds' % (time.time() - start_time))
            
    
if __name__ == '__main__':
    main()

