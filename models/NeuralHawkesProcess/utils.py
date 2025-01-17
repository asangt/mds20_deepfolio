# -*- coding: utf-8 -*-
import torch
from torch import nn
import numpy as np
import matplotlib.pyplot as plt

def create_unifrom_d(event_times, device = None):
  
    """
    Create uniform distribution of t from given event sequenses
    Input:
        event_times (batch_size, seq_len) - inter-arrival times of events
    Output:
        sim_inter_times (batch_size, seq_len) - simulated inter-arrival times of events
    """

    batch_size, batch_len = event_times.shape
    sim_inter_times = []
    tot_time_seqs = event_times.sum(dim=1)
    for tot_time in tot_time_seqs:

          # create t ∼ Unif(0, T)
          sim_time_seqs = torch.sort(torch.zeros(batch_len).uniform_(0,tot_time)).values

          # calc inter-arrival times
          sim_inter_time = torch.zeros(batch_len)
          sim_inter_time[1:] = sim_time_seqs[1:] - sim_time_seqs[:-1]
          np.random.shuffle(sim_inter_time) 
          sim_inter_times.append(sim_inter_time)

    sim_inter_times = torch.stack(sim_inter_times)
    return sim_inter_times.to(device) if device != None else sim_inter_times
  

class LogLikelihoodLoss(nn.Module):
    def __init__(self, device=None):
        super(LogLikelihoodLoss, self).__init__()
        self.device = device

    def create_unif_d(self, batch_length, total_time_seqs):

        sim_time_seqs = []
        for tot_time in total_time_seqs:
            sim_time_seqs.append(torch.rand(batch_length).uniform_(0,total_time_seqs[0]))
        sim_time_seqs = torch.stack(sim_time_seqs)
        sim_time_seqs = sim_time_seqs.transpose(1,0)
        if self.device:
            sim_time_seqs = sim_time_seqs.to(self.device)

        return sim_time_seqs

    def forward(self, model, event_seqs, time_seqs, seqs_length, total_time_seqs, output, batch_first=True):

        batch_size, batch_length = event_seqs.shape
        hidden_t, cell_t, cell_target_t, output_t, decay_t = [torch.squeeze(torch.stack(val), 0) for val in output]
        

        """ Compute log-likelihood of of the events that happened (first term) via sum of log-intensities """

        intensity = model.intensity_layer(hidden_t)
        log_intensity = intensity.log()
        #shape - S * bs * H

        original_loglikelihood = 0
        for idx, (event_seq, seq_len) in enumerate(zip(event_seqs, seqs_length)):
            arr = torch.arange(seq_len)
            pos_events = event_seq[1:seq_len+1]
            original_loglikelihood += log_intensity[arr, idx, pos_events].sum()


        """ Compute log-probabilities of non-events using Monte Carlo method (see Appendix B2) """

        ### 1) Create t ∼ Unif(0, T)
        sim_time_seqs = self.create_unif_d(batch_length, total_time_seqs)

        ### 2) Find intensities for simulated events
        hidden_t_sim = []
        for idx, sim_duration in enumerate(sim_time_seqs):
            _, h_t_sim = model.decay_cell(cell_t[idx], cell_target_t[idx], output_t[idx], decay_t[idx], sim_duration)
            hidden_t_sim.append(h_t_sim)            
        sim_intensity = model.intensity_layer(torch.stack(hidden_t_sim))

        ### 2) Caclulate integral using Monte Carlo method
        simulated_likelihood = 0
        for idx, (total_time, seq_len) in enumerate(zip(total_time_seqs, seqs_length)):
            mc_coefficient = total_time / (seq_len)
            arr = torch.arange(seq_len)
            simulated_likelihood += mc_coefficient * sim_intensity[arr, idx, :].sum(dim=(1,0))

        loglikelihood = original_loglikelihood - simulated_likelihood
        return -loglikelihood

    
def BeginningOfStream(batch_data, type_size):
    """
      While initializing LSTM we have it read a special beginning-of-stream (BOS) event (k0, t0), 
      where k0 is a special event type and t0 is set to be 0 
      (expanding the LSTM’s input dimensionality by one) see Appendix A.2
    """

    seq_events, seq_time, seq_tot_time, seqs_len = batch_data

    pad_event = torch.zeros_like(seq_events[:,0]) + type_size
    pad_time = torch.zeros_like(seq_time[:,0])
    pad_event_seqs = torch.cat((pad_event.reshape(-1,1), seq_events), dim=1)
    pad_time_seqs = torch.cat((pad_time.reshape(-1,1), seq_time), dim=1)

    return pad_event_seqs.long(), pad_time_seqs, seq_tot_time, seqs_len


from sklearn.metrics import accuracy_score, mean_squared_error

def evaluate_prediction(model, dataloader, device):
        """
        Evalute prediction on give dataset
        Will compute mse and accuracy score for event time and type prediction.
        Input:
           model - NHP model to compute decay states for sequence
           dataloader - dataloader with data
        Output:
           mean_squared_error - for event time prediction
           accuracy_score - for event type prediction
        """
        pred_data = []
        for sample in dataloader:
            event_seqs, time_seqs, total_time_seqs, seqs_length = BeginningOfStream(sample, model.type_size)
            for i in range(len(event_seqs)):
                pred_data.append(predict_event(model, time_seqs[i], event_seqs[i], seqs_length[i], device)[:4])

        pred_data = np.array(pred_data)
        time_gt, time_pred = pred_data[:,0], pred_data[:,1]
        type_gt, type_pred = pred_data[:,2], pred_data[:,3]

        time_mse_error = mean_squared_error(time_gt, time_pred)
        type_accuracy = accuracy_score(type_gt, type_pred)

        return time_mse_error, type_accuracy
    

from sklearn.metrics import accuracy_score, mean_squared_error


def predict_event(model, seq_time, seq_events, device, hmax = 40,
                     n_samples=1000):
        """ 
        Predict last event time and type for the given sequence 
        Last event takes as unknown and model feeds with all remain sequence.
        Input:
            model - NHP model to compute decay states for sequence
            seq_time - torch.tensor with time diffs between events in sequence
            seq_events - torch.tensor with event types for each time point
            seq_length - length of the sequence
        
        Output:
            pred_dt - predicted dt for next event
            gt_dt - gt df of next event
            pred_type - predicted type of next event
            gt_type - gt_type of next event
            time_between_events - np.array - generated timestamps
            intensity - np.array - intensity after event
        """

        """ Feed the model with sequence and compute decay cell state """
        seq_length = seq_time.shape[0]-1

        types_gt, types_pred = [], []
        times_gt, times_pred = [], []
        with torch.no_grad():

            h_t = torch.zeros(1, model.hidden_size, dtype=torch.float, device=device)
            c_t = torch.zeros(1, model.hidden_size, dtype=torch.float, device=device)
            c_target = torch.zeros(1, model.hidden_size, dtype=torch.float, device=device)

            for i in range(1,seq_length):
                c_t, c_target, output, decay = model.CTLSTM_cell(model.Embedding(seq_events[i].to(device)).unsqueeze(0), h_t, 
                                                               c_t, c_target)

                c_t = c_t * torch.exp(-decay * seq_time[i, None].to(device)) 
                h_t = output * torch.tanh(c_t)

                # gt last and one before last event types and times
                gt_type = seq_events[i + 1]
                gt_dt = seq_time[i+1]


                """ Make prediction for the next event time and type """
                model.eval()
                timestep = hmax / n_samples

                # 1) Compute intensity
                time_between_events = torch.linspace(0, hmax, n_samples + 1).to(device)
                hidden_vals = h_t * torch.exp(-decay * time_between_events[:, None])
                intensity = model.intensity_layer(hidden_vals.to(device))
                intensity_sum = intensity.sum(dim=1)


                # 2) Compute density via integral 
                density = torch.cumsum(timestep * intensity.sum(dim=1), dim=0)
                density = intensity_sum * torch.exp(-density)

                # 3) Predict time of the next event via trapeze method
                t = time_between_events * density   
                pred_dt = (timestep * 0.5 * (t[1:] + t[:-1])).sum() 
                # 4) Predict type of the event via trapeze method
                P = intensity / intensity_sum[:, None] * density[:, None]  

                #pred_type = torch.argmax(timestep * 0.5 * (P[1:] + P[:-1])).sum(dim=0)
                pred_type = (timestep * 0.5 * (P[1:] + P[:-1])).sum(dim=0)
                pred_type = pred_type.argmax()

                if i > 1:
                    times_gt.append(gt_dt.item())
                    times_pred.append(pred_dt.item())
                    types_gt.append(gt_type.item())
                    types_pred.append(pred_type.item())


            return times_gt, times_pred, types_gt, types_pred

        
        
def plot_stats(stats):
    fig, ax = plt.subplots(1,3,figsize=(20,5))

    train, val = np.array(stats['train']), np.array(stats['val'])
    ax[0].plot(-train[:,0], c = 'r', label = 'train')
    ax[0].plot(-val[:,0], c = 'b', label = 'val')
    ax[0].set(xlabel='Epoch', ylabel='Log-likelihood/nats', title='Log-likelihood')

    ax[1].plot(train[:,1]**0.5, c = 'r', label = 'train')
    ax[1].plot(val[:,1]**0.5, c = 'b', label = 'val')
    ax[1].set(xlabel='Epoch', ylabel='RMSE', title='time RMSE')

    ax[2].plot(train[:,3], c = 'r', label = 'train')
    ax[2].plot(val[:,3], c = 'b', label = 'val')
    ax[2].set(xlabel='Epoch', ylabel='accuracy', title='Type accuracy')
    plt.legend()
    plt.show()
