import math
import random
import numpy as np
import pandas as pd
import altair as alt

import torch
from torch.utils.data import Dataset, DataLoader

# TODO: remove this
import jax.profiler

import utils
import dataset_utils
import metrics


class LossDataEstimator:
    def __init__(self, init_fn, train_step_fn, eval_fn, dataset,
                 representation_fn=lambda x: x,
                 val_frac=0.1, n_seeds=5,
                 train_steps=5e3, batch_size=256,
                 cache_data=True, whiten=True,
                 use_vmap=True, verbose=False):
        """Create a LossDataEstimator.
        Arguments:
        - init_fn: (function int -> object)
            a function which maps from an integer random seed to an initial
            state for the training algorithm. this initial state will be fed to
            train_step_fn, and the output of train_step_fn will replace it at
            each step.
        - train_step_fn: (function (object, (ndarray, ndarray)) -> (object, num)
            a function which performs one step of training. in particular,
            should map (state, batch) -> (new_state, loss) where state is
            defined recursively, initialized by init_fn and replaced by
            train_step, and loss is a Python number.
        - eval_fn: (function (object, (ndarray, ndarray)) -> float)
            a function which takes in a state as produced by init_fn or
            train_step_fn, plus a batch of data, and returns the _mean_ loss
            over points in that batch. should not mutate anything.
        - dataset: a PyTorch Dataset or tuple (data_x, data_y).
        - representation_fn: (function ndarray -> ndarray)
            a function which takes in a batch of observations from the dataset,
            given as a numpy array, and gives back an ndarray of transformed
            observations.
        """
        self.init_fn = init_fn
        self.train_step_fn = train_step_fn
        self.eval_fn = eval_fn
        self.dataset = dataset
        self.representation_fn = representation_fn
        self.val_frac = val_frac
        self.n_seeds = n_seeds
        self.train_steps = int(train_steps)
        self.batch_size = batch_size
        self.use_vmap = use_vmap
        self.verbose = verbose
        if self.verbose:
            self.print = print
        else:
            self.print = utils.no_op

        if self.use_vmap and not cache_data:
            raise ValueError(("Setting use_vmap requires cache_data. "
                              "Either set cache_data=True or "
                              "turn off use_vmap."))

        torch.manual_seed(0)
        np.random.seed(0)
        random.seed(0)

        if not isinstance(self.dataset, Dataset):
            data_x, data_y = self.dataset
            data_x = torch.as_tensor(data_x)
            data_y = torch.as_tensor(data_y)
            self.dataset = torch.utils.data.TensorDataset(data_x, data_y)

        # Step 1: split into train and val
        self.val_size = math.ceil(len(self.dataset) * self.val_frac)
        self.max_train_size = len(self.dataset) - self.val_size
        self.train_set = dataset_utils.DatasetSubset(
            self.dataset, stop=self.max_train_size)
        self.val_set = dataset_utils.DatasetSubset(
            self.dataset, start=self.max_train_size)

        # Step 2: figure out when / if we're caching the data
        if use_vmap:
            # transform the whole training and put it in JAX
            self.train_set = utils.dataset_to_jax(
                self.train_set,
                batch_transforms=[self.representation_fn],
                batch_size=batch_size)
            self.val_set = utils.dataset_to_jax(
                self.val_set,
                batch_transforms=[self.representation_fn],
                batch_size=batch_size)
            # we've already used representation_fn to transform the data
            self.batch_transforms = []
        elif cache_data:
            # transform the data and cache it as a Pytorch dataset
            self.train_set = dataset_utils.DatasetTransformCache(
                self.train_set,
                batch_transforms=[self.representation_fn],
                batch_size=self.batch_size)
            self.val_set = dataset_utils.DatasetTransformCache(
                self.val_set,
                batch_transforms=[self.representation_fn],
                batch_size=self.batch_size)
            # we've already used representation_fn to transform
            self.batch_transforms = []
        else:
            # don't transform or cache the data yet
            # instead add representation_fn and transform one batch at a time
            self.batch_transforms = [self.representation_fn]

        # Step 3: whiten transformed data
        if whiten:
            # streams one batch at a time through batch_transforms
            mean, std = utils.compute_stats(
                self.train_set, self.batch_transforms, self.batch_size)
            self.print((f"Whitening with representation "
                        f"(mean, std): ({mean :.4f}, {std :.4f})"))
            whiten_transform = utils.make_whiten_transform(mean, std)
            self.batch_transforms.append(whiten_transform)

        self.results = pd.DataFrame(
            columns=["seed", "samples", "val_loss"])
        self.results['samples'] = self.results['samples'].astype(int)

    @jax.profiler.trace_function
    def compute_curve(self, n_points=10, sampling_type='log', points=None):
        """Computes the loss-data curve for the given algorithm and dataset.
        Arguments:
        - n_points: (int) the number of points at which the loss will be
            computed to estimate the curve
        - sampling_type: (str) how to distribute the n_points between 0 and
            len(dataset). valid options are 'log' (np.logspace) or 'linear'
            (np.linspace).
        - points: (list of ints) manually specify the exact points at which to
            estimate the loss.
        Returns: nothing.
        Effects: This LossDataEstimator instance will record the results of the
            experiments which are run, including them in the results dataframe
            and using them to compute representation quality measures.
        """
        if points is None:
            if sampling_type == 'log':
                points = np.logspace(1, np.log10(self.max_train_size), n_points)
            elif sampling_type == 'linear':
                points = np.linspace(10, self.max_train_size, n_points)
            else:
                raise ValueError((f"Argument sampling_type should be "
                                  f"'log' or 'linear', was {sampling_type}."))
            points = np.ceil(points)

        if self.use_vmap:
            return self._compute_curve_full_vmap(points)
        else:
            return self._compute_curve_sequential(points)

    def refine_esc(self, epsilon, precision, parallelism=10):
        """Runs experiments to refine an estimate of epsilon sample complexity.
        Performs experiments until the gap between an upper and lower bound is
        at most `precision`. This method is implemented as an iterative grid
        search.
        Arguments:
        - epsilon: (num) the tolerance specifying the maximum acceptable loss
            from running algorithm on dataset.
        - precision: (num) how tightly to bound eSC, in terms of
            upper_bound - lower_bound
        - parallelism: (int) the number of experiments to run in each round of
            grid search.
        """
        lower_bound, upper_bound = self._bound_esc(epsilon)
        while upper_bound - lower_bound > precision:
            points = np.linspace(lower_bound, upper_bound, parallelism)[1:-1]
            self.compute_curve(points=points)
            lower_bound, upper_bound = self._bound_esc(epsilon)
        return upper_bound

    def to_dataframe(self):
        return self.results.copy()

    @jax.profiler.trace_function
    def _compute_curve_sequential(self, points):
        for point in points:
            for seed in range(self.n_seeds):
                shuffled_data = dataset_utils.DatasetShuffle(self.train_set)
                data_subset = dataset_utils.DatasetSubset(shuffled_data,
                                                          stop=int(point))
                state = self._train(seed, data_subset)
                val_loss = self._eval(state, self.val_set)
                self.results = self.results.append({
                    'seed': seed,
                    'samples': point,
                    'val_loss': float(val_loss,)
                }, ignore_index=True)

                self.print(self.results)

    @jax.profiler.trace_function
    def _compute_curve_full_vmap(self, points):
        seeds = list(range(self.n_seeds))
        jobs = [(point, seed) for point in points for seed in seeds]
        product_points = [j[0] for j in jobs]
        product_seeds = [j[1] for j in jobs]

        multi_iterator = utils.jax_multi_iterator(
            self.train_set, self.batch_size, product_seeds, product_points)

        states = self._train_full_vmap(multi_iterator, product_seeds)
        val_losses = self._eval_vmap(states, self.val_set)
        for (job, val_loss) in zip(jobs, val_losses):
            self.results = self.results.append({
                'seed': job[1],
                'samples': job[0],
                'val_loss': float(val_loss),
            }, ignore_index=True)

        self.print(self.results)

    @jax.profiler.trace_function
    def _train_full_vmap(self, multi_iterator, seeds):
        import jax
        import jax.numpy as jnp
        vmap_train_step = jax.vmap(self.train_step_fn)
        states = jax.vmap(self.init_fn)(jnp.array(seeds))

        for step in range(self.train_steps):
            stacked_xs, stacked_ys = next(multi_iterator)
            stacked_xs = utils.apply_transforms(
                self.batch_transforms, stacked_xs)
            states, losses = vmap_train_step(states, (stacked_xs, stacked_ys))
        return states

    def _make_loader(self, dataset, shuffle):
        return DataLoader(dataset, batch_size=self.batch_size, shuffle=shuffle)

    @jax.profiler.trace_function
    def _train(self, seed, dataset):
        """Performs training with the algorithm associated with this LDE.
        Runs `self.train_steps` batches' worth of updates to the model and
        returns the state.
        """
        torch.manual_seed(seed)
        loader = self._make_loader(dataset, shuffle=True)
        state = self.init_fn(seed)
        step = 0
        while step < self.train_steps:
            for batch in loader:
                xs, ys = utils.batch_to_numpy(batch)
                xs = utils.apply_transforms(
                    self.batch_transforms, xs)
                state, loss = self.train_step_fn(state, (xs, ys))
                step += 1
                if step >= self.train_steps:
                    break
        return state

    @jax.profiler.trace_function
    def _eval(self, state, dataset):
        """Evaluates the model specified by state on dataset.
        Computes the average loss by summing the total loss over all datapoints
        and dividing.
        """
        loss, examples = 0, 0
        loader = self._make_loader(dataset, shuffle=False)
        for batch in loader:
            # careful to deal with different-sized batches
            xs, ys = utils.batch_to_numpy(batch)
            xs = utils.apply_transforms(
                self.batch_transforms, xs)
            batch_examples = xs.shape[0]
            loss += self.eval_fn(state, (xs, ys)) * batch_examples
            examples += batch_examples
        return loss / examples

    @jax.profiler.trace_function
    def _eval_vmap(self, states, dataset):
        """Evaluates
        """
        import jax
        vmap_eval = jax.vmap(self.eval_fn, in_axes=(0, None))

        # it can be hard to get the length of a state (if it's e.g. a Flax
        # Optimizer with nested parameters) so return however many losses we get
        losses = None
        examples = 0
        for i in range(0, self.val_size, self.batch_size):
            xs = self.val_set[0][i: i + self.batch_size]
            ys = self.val_set[1][i: i + self.batch_size]

            # careful to deal with different-sized batches
            xs = utils.apply_transforms(
                self.batch_transforms, xs)
            batch_examples = xs.shape[0]

            if losses is None:
                losses = vmap_eval(states, (xs, ys)) * batch_examples
            else:
                losses += vmap_eval(states, (xs, ys)) * batch_examples
            examples += batch_examples
        return losses / examples

    def _bound_esc(self, epsilon):
        """Finds an upper and lower bound for epsilon sample complexity.
        Looks through the results obtained so far.
        Finds the minimum n where loss is less than epsilon and the maximum n
        where loss is greater than epsilon.
        """
        r = self.results
        upper_bound = r[r['val_loss'] <= epsilon]['samples'].min()
        lower_bound = r[r['val_loss'] > epsilon]['samples'].max()
        if np.isnan(upper_bound):
            upper_bound = None
            lower_bound = r['samples'].max()
        elif np.isnan(lower_bound):
            lower_bound = None
            upper_bound = r['samples'].min()
        return (lower_bound, upper_bound)


def render_curve(df, save_path=None):
    title = 'Loss-data curve'
    color_title = 'Representation'
    line_width = 5
    label_size = 24
    title_size = 30

    xscale = alt.Scale(type='log')
    yscale = alt.Scale(type='log')

    x_axis = alt.X('samples', scale=xscale, title='Dataset size')
    y_axis = alt.Y('mean(val_loss)', scale=yscale, title='Validation loss')

    colorscheme = 'set1'
    stroke_color = '333'
    line = alt.Chart(df, title=title).mark_line(size=line_width, opacity=0.4)
    line = line.encode(
        x=x_axis, y=y_axis,
        color=alt.Color('name:N', title=color_title,
                        scale=alt.Scale(scheme=colorscheme,),
                        legend=None),
    )

    point = alt.Chart(df, title=title).mark_point(size=80, opacity=1)
    point = point.encode(
        x=x_axis, y=y_axis,
        color=alt.Color('name:N', title=color_title,
                        scale=alt.Scale(scheme=colorscheme,)),
        shape=alt.Shape('name:N', title=color_title),
        tooltip=['samples', 'name']
    )

    chart = alt.layer(line, point).resolve_scale(
        color='independent',
        shape='independent'
    )
    chart = chart.properties(width=600, height=500, background='white')
    chart = chart.configure_legend(labelLimit=0)
    chart = chart.configure(
        title=alt.TitleConfig(fontSize=title_size, fontWeight='normal'),
        axis=alt.AxisConfig(titleFontSize=title_size,
                            labelFontSize=label_size, grid=False,
                            domainWidth=5, domainColor=stroke_color,
                            tickWidth=3, tickSize=9, tickCount=4,
                            tickColor=stroke_color, tickOffset=0),
        legend=alt.LegendConfig(titleFontSize=title_size,
                                labelFontSize=label_size,
                                labelLimit=0, titleLimit=0,
                                orient='top-right', padding=10,
                                titlePadding=10, rowPadding=5,
                                fillColor='white', strokeColor='black',
                                cornerRadius=0),
        view=alt.ViewConfig(strokeWidth=0, stroke=stroke_color)
    )
    if save_path is not None:
        chart.save(save_path)
    return chart


def compute_metrics(df, list_of_ns, list_of_epsilons):
    return metrics.compute_all(df, list_of_ns, list_of_epsilons)


def render_latex(metrics_df, display=False, save_path=None):
    """Given a df of metrics from `compute_metrics`, renders a latex table.
    """
    metrics_df.index = metrics_df.index.str.replace('eps', '$\\\\varepsilon$')
    metrics_df.index = metrics_df.index.str.replace('eSC',
                                                    '$\\\\varepsilon$SC')
    metrics_df = metrics_df.stack()
    metrics_df = metrics_df.swaplevel().sort_values('n', ascending=True)

    latex_str = metrics_df.to_latex(multicolumn_format='c',
                                    float_format="{:0.2f}".format,
                                    escape=False,
                                    column_format='llrrr')
    if save_path is not None:
        metrics_df.to_latex(save_path,
                            multicolumn_format='c',
                            float_format="{:0.2f}".format,
                            escape=False,
                            column_format='llrrr')
    if display:
        import ipywidgets as widgets
        import IPython.display
        out = widgets.Output(layout={'border': '1px solid black'})
        out.append_stdout(latex_str)
        IPython.display.display(out)
    return latex_str
