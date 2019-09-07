#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Created on Wed Aug 14 10:20:21 2019

@author: jonas and evan
"""

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils import data as D
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt
from hypergrad import AdamHD
from time import time


class DataWrapper(object):
    def __init__(self, df, continuous_vars=[], categorical_vars=[], context_vars=[],
                 continuous_lower_bounds = dict(), continuous_upper_bounds = dict()):
        variables = dict(continuous=continuous_vars,
                         categorical=categorical_vars,
                         context=context_vars)
        self.variables = variables
        continuous, context = [torch.tensor(np.array(df[variables[_]])).to(torch.float) for _ in ("continuous", "context")]
        self.means = [x.mean(0, keepdim=True) for x in (continuous, context)]
        self.stds  = [x.std(0,  keepdim=True) + 1e-5 for x in (continuous, context)]
        self.cat_dims = [df[v].nunique() for v in variables["categorical"]]
        self.cont_bounds = [[continuous_lower_bounds[v] if v in continuous_lower_bounds.keys() else -1e8 for v in variables["continuous"]],
                            [continuous_upper_bounds[v] if v in continuous_upper_bounds.keys() else 1e8 for v in variables["continuous"]]]
        self.cont_bounds = (torch.tensor(self.cont_bounds).to(torch.float) - self.means[0]) / self.stds[0]

    def preprocess(self, df):
        continuous, context = [torch.tensor(np.array(df[self.variables[_]])).to(torch.float) for _ in ("continuous", "context")]
        continuous, context = [(x-m)/s for x,m,s in zip([continuous, context], self.means, self.stds)]
        if len(self.variables["categorical"]) > 0: 
            categorical = torch.tensor(pd.get_dummies(df[self.variables["categorical"]], columns=self.variables["categorical"]).to_numpy())
            return torch.cat([continuous, categorical.to(torch.float)], -1), context
        else:
            return continuous, context

    def deprocess(self, x, context):
        continuous, categorical = x.split((self.means[0].size(-1), sum(self.cat_dims)), -1)
        continuous, context = [x*s+m for x,m,s in zip([continuous, context], self.means, self.stds)]
        if categorical.size(-1) > 0: categorical = torch.cat([torch.multinomial(p, 1) for p in categorical.split(self.cat_dims, -1)], -1)
        df = pd.DataFrame(dict(zip(self.variables["continuous"] + self.variables["categorical"] + self.variables["context"],
                                   torch.cat([continuous, categorical.to(torch.float), context], -1).detach().t())))
        return df

    def apply_generator(self, generator, df):
        # replaces columns in df with data from generator wherever possible
        generator.to("cpu")
        original_columns = df.columns
        x, context = self.preprocess(df)
        x_hat = generator(context)
        df_hat = self.deprocess(x_hat, context)
        updated = self.variables["continuous"] + self.variables["categorical"]
        not_updated = [col for col in list(df_hat.columns) if col not in updated]
        df_hat = df_hat.drop(not_updated, axis=1).reset_index(drop=True)
        df = df.drop(updated, axis=1).reset_index(drop=True)
        return df_hat.join(df)[original_columns]

    def apply_critic(self, critic, df, colname="critic"):
        # adds a column "colname" containing value of critic at obs
        critic.to("cpu")
        x, context = self.preprocess(df)
        c = critic(x, context).detach()
        if colname in list(df.columns): df = df.drop(colname, axis=1)
        df.insert(0, colname, c[:, 0].numpy())
        return df


class Specifications(object):
    def __init__(self, data_wrapper, 
                 critic_d_hidden = [],
                 critic_dropout = 0.1,
                 critic_steps = 15,
                 critic_lr = 1e-4,
                 critic_gp_factor = 5,
                 generator_d_hidden = [],
                 generator_dropout = 0.1,
                 generator_lr = 1e-4,
                 generator_d_noise = "generator_d_output",
                 optimizer = "AdamHD",
                 max_epochs = 1000,
                 batch_size = 32,
                 test_set_size = 16,
                 load_checkpoint = None,
                 save_checkpoint = None,
                 save_every = 1,
                 print_every = 1,
                 device = "cuda" if torch.cuda.is_available() else "cpu"):

        self.settings = locals()
        del self.settings["self"], self.settings["data_wrapper"]
        d_context = len(data_wrapper.
                        variables["context"])
        d_cont = len(data_wrapper.variables["continuous"])
        d_x = d_cont + sum(data_wrapper.cat_dims)
        if generator_d_noise == "generator_d_output":
            self.settings.update(generator_d_noise = d_x)
        self.data = dict(d_context=d_context, d_x=d_x, 
                         cat_dims=data_wrapper.cat_dims,
                         cont_bounds=data_wrapper.cont_bounds)

        print("settings:", self.settings)


class Generator(nn.Module):
    def __init__(self, specifications):
        super().__init__()
        s, d = specifications.settings, specifications.data
        self.cont_bounds = d["cont_bounds"]
        self.cat_dims = d["cat_dims"]
        self.d_cont = self.cont_bounds.size(-1)
        self.d_cat = sum(d["cat_dims"])
        self.d_noise = s["generator_d_noise"]
        d_in = [self.d_noise + d["d_context"]] + s["generator_d_hidden"]
        d_out = s["generator_d_hidden"] + [self.d_cont + self.d_cat]
        self.layers = nn.ModuleList([nn.Linear(i, o) for i, o in zip(d_in, d_out)])
        self.dropout = nn.Dropout(s["generator_dropout"])

    def transform(self, hidden):
        continuous, categorical = hidden.split([self.d_cont, self.d_cat], -1)
        # apply bounds to continuous
        bounds = self.cont_bounds.to(hidden.device)
        continuous = torch.stack([continuous, bounds[0:1].expand_as(continuous)]).max(0).values
        continuous = torch.stack([continuous, bounds[1:2].expand_as(continuous)]).min(0).values
        # renormalize categorical
        if categorical.size(-1) > 0: categorical = torch.cat([F.softmax(x, -1) for x in categorical.split(self.cat_dims, -1)], -1)
        return torch.cat([continuous, categorical], -1)

    def forward(self, context):
        noise = torch.randn(context.size(0), self.d_noise).to(context.device)
        x = torch.cat([noise, context], -1)
        for layer in self.layers[:-1]:
            x = self.dropout(F.relu(layer(x)))
        return self.transform(self.layers[-1](x))


class Critic(nn.Module):
    def __init__(self, specifications):
        super().__init__()
        s, d = specifications.settings, specifications.data
        d_in = [d["d_x"] + d["d_context"]] + s["critic_d_hidden"]
        d_out = s["critic_d_hidden"] + [1]
        self.layers = nn.ModuleList([nn.Linear(i, o) for i, o in zip(d_in, d_out)])
        self.dropout = nn.Dropout(s["critic_dropout"])

    def forward(self, x, context):
        x = torch.cat([x, context], -1)
        for layer in self.layers[:-1]:
            x = self.dropout(F.relu(layer(x)))
        return self.layers[-1](x)

    def gradient_penalty(self, x, x_hat, context):
        alpha = torch.randn(x.size(0)).unsqueeze(1).to(x.device)
        interpolated = x * alpha + x_hat * (1 - alpha)
        interpolated = torch.autograd.Variable(interpolated.detach(), requires_grad=True)
        critic = self(interpolated, context)
        gradients = torch.autograd.grad(critic, interpolated, torch.ones_like(critic),
                                        retain_graph=True, create_graph=True, only_inputs=True)[0]
        penalty = F.relu(gradients.norm(2, dim=1) - 1).mean()             # one-sided
        # penalty = (gradients.norm(2, dim=1) - 1).pow(2).mean()          # two-sided
        return penalty


def train(generator, critic, x, context, specifications):
    # setup training objects
    s = specifications.settings
    start_epoch, step, description, device, t = 0, 1, "", s["device"], time()
    generator.to(device), critic.to(device)
    opt = {"AdamHD": AdamHD, "Adam": torch.optim.Adam}[s["optimizer"]]
    opt_generator = opt(generator.parameters(), lr=s["generator_lr"])
    opt_critic = opt(critic.parameters(), lr=s["critic_lr"])
    train_batches, test_batches = D.random_split(D.TensorDataset(x, context), (x.size(0)-s["test_set_size"], s["test_set_size"]))
    train_batches, test_batches = (D.DataLoader(d, s["batch_size"], shuffle=True) for d in (train_batches, test_batches))

    # load checkpoints
    if s["load_checkpoint"]:
        cp = torch.load(s["load_checkpoint"])
        generator.load_state_dict(cp["generator_state_dict"])
        opt_generator.load_state_dict(cp["opt_generator_state_dict"])
        critic.load_state_dict(cp["critic_state_dict"])
        opt_critic.load_state_dict(cp["opt_critic_state_dict"])
        start_epoch, step = cp["epoch"], cp["step"]
    # start training
    for epoch in range(start_epoch, s["max_epochs"]):
        # train loop
        WD_train, n_batches = 0, 0
        for x, context in train_batches:
            x, context = x.to(device), context.to(device)
            generator_update = step % s["critic_steps"] == 0
            for par in critic.parameters():
                par.requires_grad = not generator_update
            for par in generator.parameters():
                par.requires_grad = generator_update
            if generator_update:
                generator.zero_grad()
            else:
                critic.zero_grad()
            x_hat = generator(context)
            critic_x_hat = critic(x_hat, context).mean()
            if not generator_update:
                critic_x = critic(x, context).mean()
                WD = critic_x - critic_x_hat
                loss = - WD + s["critic_gp_factor"] * critic.gradient_penalty(x, x_hat, context)
                loss.backward()
                opt_critic.step()
                WD_train += WD.item()
                n_batches += 1
            else:
                loss = - critic_x_hat
                loss.backward()
                opt_generator.step()
            step += 1
        WD_train /= n_batches
        # test loop
        WD_test, n_batches = 0, 0
        for x, context in test_batches:
            x, context = x.to(device), context.to(device)
            with torch.no_grad():
                x_hat = generator(context)
                critic_x_hat = critic(x_hat, context).mean()
                critic_x = critic(x, context).mean()
                WD_test += (critic_x - critic_x_hat).item()
                n_batches += 1
        WD_test /= n_batches
        # diagnostics
        if epoch % s["print_every"] == 0: 
            description = "epoch {} | step {} | WD_test {} | WD_train {} | sec passed {} |".format(
            epoch, step, round(WD_test, 2), round(WD_train, 2), round(time() - t))
            print(description)
            t = time()
        if s["save_checkpoint"] and epoch % s["save_every"] == 0:
            torch.save({"epoch": epoch, "step": step,
                        "generator_state_dict": generator.state_dict(),
                        "critic_state_dict": critic.state_dict(),
                        "opt_generator_state_dict": opt_generator.state_dict(),
                        "opt_critic_state_dict": opt_critic.state_dict()}, s["save_checkpoint"])


def compare_dfs(df_real, df_fake, scatterplot=dict(x=[], y=[], samples=400),
                table_groupby=[], histogram=dict(variables=[], nrow=1, ncol=1),
                figsize=3):
    # data prep
    if "source" in list(df_real.columns): df_real = df_real.drop("source", axis=1)
    if "source" in list(df_fake.columns): df_fake = df_fake.drop("source", axis=1)
    df_real.insert(0, "source", "real"), df_fake.insert(0, "source", "fake")
    common_cols = [c for c in df_real.columns if c in df_fake.columns]
    df_joined = pd.concat([df_real[common_cols], df_fake[common_cols]], axis=0, ignore_index=True)
    df_real, df_fake = df_real.drop("source", axis=1), df_fake.drop("source", axis=1)
    common_cols = [c for c in df_real.columns if c in df_fake.columns]
    # mean and std table
    print("-------------comparison of means-------------")
    means = df_joined.groupby(table_groupby + ["source"]).mean().round(2).transpose()
    print(means)
    print("-------------comparison of stds-------------")
    stds = df_joined.groupby(table_groupby + ["source"]).std().round(2).transpose()
    print(stds)
    # covariance matrix comparison
    fig1 = plt.figure(figsize=(figsize * 2, figsize * 1))
    s1 = [fig1.add_subplot(1, 2, i) for i in range(1, 3)]
    s1[0].set_xlabel("real")
    s1[1].set_xlabel("fake")
    s1[0].matshow(df_real[common_cols].corr())
    s1[1].matshow(df_fake[common_cols].corr())
    # histogram marginals
    if histogram and len(histogram["variables"]) > 0:
        fig2, axarr2 = plt.subplots(histogram["nrow"], histogram["ncol"],
                                    figsize=(histogram["nrow"]*figsize, histogram["ncol"]*figsize))
        v = 0
        for i in range(histogram["nrow"]): 
            for j in range(histogram["ncol"]): 
                plot_var, v = histogram["variables"][v], v+1
                axarr2[i][j].hist([df_real[plot_var], df_fake[plot_var]], bins=8, density=1,
                                  histtype='bar', label=["real", "fake"], color=["blue", "red"])
                axarr2[i][j].legend(prop={"size": 10})
                axarr2[i][j].set_title(plot_var)
        fig2.show()
    # scatterplot grid
    if scatterplot and len(scatterplot["x"]) * len(scatterplot["y"]) > 0:
        df_real_sample = df_real.sample(scatterplot["samples"])
        df_fake_sample = df_fake.sample(scatterplot["samples"])
        x_vars, y_vars = scatterplot["x"], scatterplot["y"]
        fig3 = plt.figure(figsize=(len(x_vars) * figsize, len(y_vars) * figsize))
        s3 = [fig3.add_subplot(len(y_vars), len(x_vars), i + 1) for i in range(len(x_vars) * len(y_vars))]
        for y in y_vars:
            for x in x_vars:
                s = s3.pop(0)
                x_real, y_real = df_real_sample[x].to_numpy(),  df_real_sample[y].to_numpy()
                x_fake, y_fake = df_fake_sample[x].to_numpy(), df_fake_sample[y].to_numpy()
                s.scatter(x_real, y_real, color="blue")
                s.scatter(x_fake, y_fake, color="red")
                s.set_ylabel(y)
                s.set_xlabel(x)
        fig3.show()


if __name__ == "__main__":
    file = "data/original_data/cps_merged.feather"
    df = pd.read_feather(file)

    continuous_vars = ["age", "education", "re74", "re75", "re78"]
    continuous_lower_bounds = {"re74": 0, "re75": 0, "re78": 0}
    categorical_vars = ["black", "hispanic", "married", "nodegree"]
    context_vars = ["t"]

    data_wrapper = DataWrapper(df, continuous_vars, categorical_vars, context_vars, continuous_lower_bounds)
    x, context = data_wrapper.preprocess(df)
    
    specifications = Specifications(data_wrapper)

    generator = Generator(specifications)
    critic = Critic(specifications)

    train(generator, critic, x, context, specifications)
    
    df = data_wrapper.apply_critic(critic, df, colname="critic")
    df_fake = data_wrapper.apply_generator(generator, df.sample(int(1e5), replace=True))
    df_fake = data_wrapper.apply_critic(critic, df_fake, colname="critic")
    
    compare_dfs(df, df_fake, 
                scatterplot=dict(x=["t", "age", "education", "re74", "married"],
                                 y=["re78", "critic"], samples=400),
                table_groupby=["t"],
                histogram=dict(variables=['black', 'hispanic', 'married', 'nodegree',
                                          're74', 're75', 're78', 'education', 'age'],
                               nrow=3, ncol=3),
                figsize=3)