import collections
import itertools

import pandas as pd

from rev import dist


__all__ = ['BayesNet']


@pd.api.extensions.register_series_accessor('cpt')
class CPTAccessor:

    def __init__(self, pandas_series):
        self._series = pandas_series

    def sample(self):
        return self._series.sample(weights=self._series).index[0]


class BayesNet:
    """Bayesian network.

    """

    def __init__(self, *edges):

        # Convert edges into children and parent connections
        parents = collections.defaultdict(list)
        children = collections.defaultdict(list)
        for parent, child in set(edges):
            parents[child].append(parent)
            children[parent].append(child)
        self.parents = dict(parents)
        self.children = dict(children)

        self.nodes = {*parents.keys(), *children.keys()}
        self.cpts = {}

    def _sample(self, init=None):
        """

        This method is not public because setting fixed values doesn't produce samples that follow
        the network's distribution, which is an easy pit to fall into for users.

        """

        def sample_node_value(node, sample, visited):

            # If this node has been visisted, then that implies
            # that it's parents and ancestors have been too, which means
            # there is no point going further up the network.
            if node in visited:
                return
            visited.add(node)

            # At this point, we need to ensure that the sample contains a value for each
            # parent node
            for parent in self.parents.get(node, ()):
                sample_node_value(node=parent, sample=sample, visited=visited)

            if node not in sample:
                cpt = self.cpts[node]
                if node in self.parents:
                    condition = tuple(sample[parent] for parent in self.parents[node])
                    cpt = cpt.loc[condition]
                sample[node] = cpt.cpt.sample()

        sample = init.copy() if init else {}
        visited = set()

        for leaf in self.nodes - set(self.children):
            sample_node_value(node=leaf, sample=sample, visited=visited)

        return sample

    def sample(self, n=1):
        """Generate a new sample at random by using forward sampling.

        Although the idea is to implement forward sampling, the implementation
        actually works backwards, starting from the leaf nodes. For every node, we recursively
        check that values have been sampled for each parent node. Once a value has been chosen for
        each parent, we can pick the according distribution and sample from it.

        Parameters:
            n: Number of samples to produce. A dataframe is returned if `n > 1`. A dictionary is
                returned if `n <= 1`.

        """
        if n > 1:
            return pd.DataFrame(self._sample() for _ in range(n))
        return self._sample()

    def fit(self, X: pd.DataFrame):
        """Find the values of each conditional distribution."""

        # Compute conditional distribution for each child
        for child, parents in self.parents.items():
            self.cpts[child] = X.groupby(parents)[child].value_counts(normalize=True)
            self.cpts[child].name = f'P({child} | {", ".join(parents)})'

        # Compute distribution for each orphan (i.e. the roots)
        for orphan in self.nodes - set(self.parents):
            self.cpts[orphan] = X[orphan].value_counts(normalize=True)
            self.cpts[orphan].index.name = orphan
            self.cpts[orphan].name = f'P({orphan})'

        return self

    def _rejection_sampling(self, *query, event, n):
        """Answer a query using rejection sampling.

        """

        # We don't know many samples we won't reject, therefore we cannot preallocate arrays
        samples = {var: [] for var in query}

        for _ in range(n):
            sample = self.sample()

            # Reject if the sample is not consistent with the specified events
            if any(sample[var] != val for var, val in event.items()):
                continue

            for var in query:
                samples[var].append(sample[var])

        samples = pd.DataFrame(samples)
        return samples.groupby(list(query)).size() / len(samples)

    def _llh_weighting(self, *query, event, n):
        """Answers a query using likelihood weighting.

        Likelihood weighting is a particular instance of importance sampling.

        """

        samples = {var: [None] * n for var in query}
        weights = [None] * n

        for i in range(n):

            # Sample by using the events as fixed values
            sample = self._sample(init=event)

            # Compute the likelihood of this sample
            weight = 1.
            for var, val in event.items():
                condition = tuple(sample[p] for p in self.parents.get(var, ()))
                weight *= self.cpts[var].loc[condition].get(val, 0)

            for var in query:
                samples[var][i] = sample[var]
                weights[i] = weight

        results = pd.DataFrame({'weight': weights, **samples})
        results = results.groupby(list(query))['weight'].mean()
        results /= results.sum()

        return results

    def _gibbs_sampling(self, *query, event, n):
        """Gibbs sampling.

        """

        # We start by computing the conditional distributions for each node that is not part of
        # the event. Each relevant node is therefore conditioned on its Markov blanket. Refer to
        # equation 14.12 of Artificial Intelligence: A Modern Approach for more detail.
        posteriors = {}
        nonevents = self.nodes - set(event)
        for node in nonevents:
            posterior = self.cpts[node]
            for child in self.children.get(node, ()):
                posterior = posterior * self.cpts[child]  # in-place mul doesn't work
            by = posterior.index.names[:-1]
            posterior = posterior.groupby(by).apply(lambda g: g / g.sum())
            posteriors[node] = posterior

        # Initialize a sample
        sample = self._sample(init=event)

        samples = {var: [None] * n for var in query}
        queue = itertools.cycle(nonevents)  # arbitrary order

        for i in range(n):
            var = next(queue)
            cpt = posteriors[var]
            condition = tuple(sample[p] for p in posteriors[var].index.names[:-1])
            if condition:
                cpt = cpt.loc[condition]
            val = cpt.cpt.sample()
            sample[var] = val

            for var in query:
                samples[var][i] = sample[var]

        samples = pd.DataFrame(samples)
        return samples.groupby(list(query)).size() / len(samples)

    def query(self, *query, event, algorithm='rejection', n=100):
        if algorithm == 'likelihood':
            answer = self._llh_weighting(*query, event=event, n=n)

        elif algorithm == 'rejection':
            answer = self._rejection_sampling(*query, event=event, n=n)

        else:
            raise ValueError('Unknown algorithm, must be one of: likelihood, rejection')

        return answer.rename(f'P({",".join(query)})')


    def impute(self, sample):
        raise NotImplementedError
