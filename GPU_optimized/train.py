import torch
import torch.nn as nn
import torch.optim as optim
import numpy as np
import time
import math
import random
import torch.nn.functional as F
import torch.jit as jit
import os, re


#==============================================================================================
#==============================================================================================
#=========================================LOGGING=============================================
#==============================================================================================
#==============================================================================================

device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
# to continue writing to the same history file and derive its name. This function created with the help of ChatGPT
def extract_r1_r2_r3():
    pattern = r'history_(\d+)_(\d+)_(\d+)\.log'

    # Iterate through the files in the given directory
    for filename in os.listdir():
        # Match the filename with the pattern
        match = re.match(pattern, filename)
        if match:
            # Extract the numbers r1, r2, and r3 from the filename
            return map(int, match.groups())
    return None


#write or append to the history log file
class LogFile(object):
    def __init__(self, log_name_main, log_name_opt):
        self.log_name_main = log_name_main
        self.log_name_opt = log_name_opt
    def write(self, text):
        with open(self.log_name_main, 'a+') as file:
            file.write(text)
    def write_opt(self, text):
        with open(self.log_name_opt, 'a+') as file:
            file.write(text)
    def clean(self):
        with open(self.log_name_main, 'w') as file:
            file.write("")
        with open(self.log_name_opt, 'w') as file:
            file.write("")


numbers = extract_r1_r2_r3()
if numbers != None:
    # derive random numbers from history file
    r1, r2, r3 = numbers
else:
    # generate new random seeds
    r1, r2, r3 = random.randint(0,10), random.randint(0,10), random.randint(0,10)

torch.manual_seed(r1)
np.random.seed(r2)
random.seed(r3)

print(r1, ", ", r2, ", ", r3)

log_name_main = "history_" + str(r1) + "_" + str(r2) + "_" + str(r3) + ".log"
log_name_opt = "episodes_" + str(r1) + "_" + str(r2) + "_" + str(r3) + ".log"
log_file = LogFile(log_name_main, log_name_opt)




#==============================================================================================
#==============================================================================================
#=========================================SYMPHONY=============================================
#==============================================================================================
#==============================================================================================




#Rectified Huber Symmetric Error Loss Function via JIT Module
# nn.Module -> JIT C++ graph
class ReHSE(jit.ScriptModule):
    def __init__(self):
        super(ReHSE, self).__init__()

    @jit.script_method
    def forward(self, e, k:float):
        ae = torch.abs(e) + 1e-6
        ae = ae**k*torch.tanh(k*ae/2)
        return ae.mean()


#Rectified Huber Asymmetric Error Loss Function via JIT Module
# nn.Module -> JIT C++ graph
class ReHAE(jit.ScriptModule):
    def __init__(self):
        super(ReHAE, self).__init__()

    @jit.script_method
    def forward(self, e, k:float):
        e = e + 1e-6
        e = torch.abs(e)**k*torch.tanh(k*e/2)
        return e.mean()





#Linear Layer followed by Silent Dropout
# nn.Module -> JIT C++ graph
class LinearSDropout(jit.ScriptModule):
    def __init__(self, f_in, f_out, p=0.5):
        super(LinearSDropout, self).__init__()
        self.ffw = nn.Linear(f_in, f_out)
        self.p = p

    @jit.script_method
    def forward(self, x):
        x = self.ffw(x)
        #Silent Dropout function created with the help of ChatGPT
        # It is not recommended to use JIT compilation decorator with online random generator as Symphony updates seeds each time
        # We did exception only for this module as it is used inside neural networks.
        mask = (torch.rand_like(x) > self.p).float()
        return  mask * x + (1.0-mask) * x.detach()



#ReSine Activation Function
# nn.Module -> JIT C++ graph
class ReSine(jit.ScriptModule):
    def __init__(self, hidden_dim=256):
        super(ReSine, self).__init__()
        self.s = nn.Parameter(data=2.0*torch.rand(hidden_dim)-1.0, requires_grad=True)

    @jit.script_method
    def forward(self, x):
        s = torch.sigmoid(self.s)
        x = s*torch.sin(x/s)
        return x/(1+torch.exp(-1.5*x/s))






#Shared Feed Forward Module
# nn.Module -> JIT C++ graph
class FeedForward(jit.ScriptModule):
    def __init__(self, f_in, f_out):
        super(FeedForward, self).__init__()

        self.ffw = nn.Sequential(
            nn.Linear(f_in, 320),
            nn.LayerNorm(320),
            nn.Linear(320, 256),
            ReSine(),
            LinearSDropout(256, 192, 0.5),
            LinearSDropout(192, f_out, 0.5)
        )

    @jit.script_method
    def forward(self, x):
        return self.ffw(x)



# nn.Module -> JIT C++ graph
class ActorCritic(jit.ScriptModule):
    def __init__(self, state_dim, action_dim, max_action=1.0):
        super().__init__()

        
        self.inA = nn.Linear(state_dim, 320)
        self.inB = nn.Linear(state_dim, 256)
        self.inC = nn.Linear(state_dim, 192)

        self.a = FeedForward(768+state_dim, action_dim)

        self.qdim=320

        self.qA = FeedForward(state_dim+action_dim, 320)
        self.qB = FeedForward(state_dim+action_dim, 320)
        self.qC = FeedForward(state_dim+action_dim, 320)

        self.qnets = nn.ModuleList([self.qA, self.qB, self.qC])

        self.max_action = nn.Parameter(data=max_action, requires_grad=False)

        self.window = 0.333

        self.max_limit = nn.Parameter(data=self.window*self.max_action, requires_grad=False)
        self.lin = nn.Parameter(data=0.7*self.max_limit, requires_grad=False)
        self.std = nn.Parameter(data=0.3*self.max_limit, requires_grad=False)


    @jit.script_method
    def oxygen(self):
        self.window += 5e-6
        self.max_limit.copy_(min(self.window, 1)*self.max_action)
        self.lin.copy_(0.7*self.max_limit)
        self.std.copy_(0.3*self.max_limit)

    @jit.script_method
    def actor(self, state):
        state = torch.cat([self.inA(state), self.inB(state), self.inC(state), state], dim=-1)
        x = self.a(state)
        return self.max_limit*torch.tanh(x/self.max_limit)

    @jit.script_method
    def critic(self, state, action):
        x = torch.cat([state, action], -1)
        return [qnet(x) for qnet in self.qnets]


    @jit.script_method
    def squash(self, x):
        shift = torch.sign(x)*self.lin
        return self.std * torch.tanh((x-shift)/self.std) + shift
    
    # Do not use any decorators with online random generators (Symphony updates seed each time)
    def actor_soft(self, state):
        x = self.actor(state)
        x += (self.std*torch.randn_like(x)).clamp(-2.5*self.std, 2.5*self.std)
        return torch.where(torch.abs(x)<self.lin, x, self.squash(x))


    # take 3 distributions and concatenate them
    @jit.script_method
    def critic_soft(self, state, action):
        q = torch.cat(self.critic(state, action), dim=-1)
        q_mean = torch.mean(q, dim=-1, keepdim=True)
        s = (q_mean-q)/self.qdim
        return q_mean, s*torch.tanh(s)




# Define the algorithm
class Symphony(object):
    def __init__(self, state_dim, action_dim, device, max_action=1.0, tau=0.005, capacity=300000, batch_lim = 768):

        self.replay_buffer = ReplayBuffer(state_dim, action_dim, device, capacity, batch_lim)

        self.nets = ActorCritic(state_dim, action_dim, max_action=max_action,).to(device)
        self.nets_target = ActorCritic(state_dim, action_dim, max_action=max_action,).to(device)
        self.nets_target.load_state_dict(self.nets.state_dict())


        self.nets_optimizer = optim.RMSprop(self.nets.parameters(), lr=3e-4)

        self.rehse = ReHSE()
        self.rehae = ReHAE()


        self.max_action = max_action

        self.tau = tau
        self.tau_ = 1.0 - tau
        self.state_dim = state_dim
        self.action_dim = action_dim
        self.device = device

        self.scaler = torch.amp.GradScaler("cuda")




    def select_action(self, state, mean=False):
        state = torch.FloatTensor(state).reshape(-1,self.state_dim).to(self.device)
        with torch.no_grad(): action = self.nets_target.actor(state) if mean else self.nets_target.actor_soft(state)
        return action.cpu().data.numpy().flatten()




    def train(self, tr_per_step=5):
        # decreases dependence on random seeds:
        r1, r2, r3 = random.randint(0,2**32-1), random.randint(0,2**32-1), random.randint(0,2**32-1)
        torch.manual_seed(r1)
        np.random.seed(r2)
        random.seed(r3)

        for _ in range(tr_per_step): self.update()



    def update(self):

        state, action, reward, next_state, done = self.replay_buffer.sample()
        self.nets_optimizer.zero_grad(set_to_none=True)
        k = 0.5*self.replay_buffer.power
        self.nets.oxygen()

        with torch.amp.autocast("cuda", dtype=torch.bfloat16):
            next_action = self.nets.actor_soft(next_state)
            q_next_target, s2_next_target  = self.nets_target.critic_soft(next_state, next_action)
            q = 0.01 * reward + (1-done) * 0.99 * q_next_target.detach()
            qs = self.nets.critic(state, action)


            actor_loss = -1/10*(self.rehae(q_next_target, k) + self.rehse(s2_next_target, k))
            critic_loss = (self.rehse(q-qs[0], k) + self.rehse(q-qs[1], k) + self.rehse(q-qs[2], k))

            nets_loss = actor_loss + critic_loss

        """ 
        nets_loss.backward()
        self.nets_optimizer.step()
        """

        self.scaler.scale(nets_loss).backward()
        self.scaler.step(self.nets_optimizer)
        self.scaler.update()

        with torch.no_grad():
            for target_param, param in zip(self.nets_target.parameters(), self.nets.parameters()):
                target_param.data.copy_(self.tau_*target_param.data + self.tau*param.data)

       



class ReplayBuffer:
    def __init__(self, state_dim, action_dim, device, capacity, batch_lim):

        self.capacity, self.length, self.batch_lim, self.device = capacity, 0, batch_lim, device
        self.batch_size = 32 + self.length//1000 #in order for sample to describe population
        self.random = np.random.default_rng()
        self.indices, self.indexes, self.probs = [], np.array([]), np.array([])
        self.power = 0.0

        self.states = torch.zeros((self.capacity, state_dim), dtype=torch.bfloat16, device=device)
        self.actions = torch.zeros((self.capacity, action_dim), dtype=torch.bfloat16, device=device)
        self.rewards = torch.zeros((self.capacity, 1), dtype=torch.bfloat16, device=device)
        self.next_states = torch.zeros((self.capacity, state_dim), dtype=torch.bfloat16, device=device)
        self.dones = torch.zeros((self.capacity, 1), dtype=torch.bfloat16, device=device)


    #Normalized index conversion into fading probabilities
    def fade(self, norm_index):
        weights = np.tanh(10*norm_index**2.7) # linear / -> non-linear _/‾
        return weights/np.sum(weights) #probabilities



    def add(self, state, action, reward, next_state, done):

        

        idx = self.length-1
        if self.length<self.capacity:
            self.length += 1
            self.indices.append(self.length-1)
            self.indexes = np.array(self.indices)
            self.probs = self.fade(self.indexes/self.length) if self.length>1 else np.array([0.0])
            self.batch_size = 32 + self.length//1000
            self.power = self.length/self.capacity
            
            

        
        self.states[idx,:] = torch.tensor(state, dtype=torch.bfloat16, device=self.device)
        self.actions[idx,:] = torch.tensor(action, dtype=torch.bfloat16, device=self.device)
        self.rewards[idx,:] = torch.tensor([reward], dtype=torch.bfloat16, device=self.device)
        self.next_states[idx,:] = torch.tensor(next_state, dtype=torch.bfloat16, device=self.device)
        self.dones[idx,:] = torch.tensor([done], dtype=torch.bfloat16, device=self.device)


        if self.length==self.capacity:
            self.states = torch.roll(self.states, shifts=-1, dims=0)
            self.actions = torch.roll(self.actions, shifts=-1, dims=0)
            self.rewards = torch.roll(self.rewards, shifts=-1, dims=0)
            self.next_states = torch.roll(self.next_states, shifts=-1, dims=0)
            self.dones = torch.roll(self.dones, shifts=-1, dims=0)



   
    # Do not use any decorators with random generators (Symphony updates seed each time)
    def sample(self):
        indices = self.random.choice(self.indexes, p=self.probs, size=self.batch_size)

        return (
            self.states[indices],
            self.actions[indices],
            self.rewards[indices],
            self.next_states[indices],
            self.dones[indices]
        )


    def __len__(self):
        return self.length
