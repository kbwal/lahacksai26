"""DPO on (draft, edited final) pairs, mixed with SFT, on top of mlx_lm.tuner.

Every step does one forward pass over [SFT rows; chosen rows; rejected rows].
The SFT rows get the usual masked NLL. The chosen/rejected rows get the DPO
loss against reference log-probs that are computed once, before training, from
the starting model: either the base model with a fresh adapter (LoRA B is
zero-initialized, so that is exactly the base model) or the adapter being
resumed, i.e. the model that wrote the drafts. Precomputing them means we never
hold a second copy of the model in memory.

Rows carry a per-token loss mask rather than a prompt length, since one
conversation can have several "me" messages to train on.

`MixedDataset` and `iterate_mixed_batches` plug into mlx_lm's `train` and
`evaluate` through their `iterate_batches` argument, and `mixed_loss` through
`loss`.
"""

from dataclasses import dataclass

import mlx.core as mx
import mlx.nn as nn
import numpy as np

SftExample = tuple[list[int], list[int]]  # (tokens, loss mask), see train/format.py
DpoExample = tuple[SftExample, SftExample]  # (chosen, rejected)


@dataclass
class MixedDataset:
    """Example pools and how many rows each step draws from each.

    `sft` is a list of (pool, rows per step), so new edits can be oversampled
    against replayed older pairs. `dpo` carries one (ref_chosen, ref_rejected)
    pair of reference log-probs per example in `ref`.
    """

    sft: list[tuple[list[SftExample], int]]
    dpo: list[DpoExample]
    ref: list[tuple[float, float]]
    dpo_batch: int
    seed: int = 0

    @property
    def batch_size(self) -> int:
        return sum(k for _, k in self.sft) + (self.dpo_batch if self.dpo else 0)

    def epoch_batches(self) -> int:
        streams = [(len(pool), k) for pool, k in self.sft]
        if self.dpo:
            streams.append((len(self.dpo), self.dpo_batch))
        return max(-(-n // k) for n, k in streams)

    def __len__(self) -> int:
        # mlx_lm only uses len(dataset) // batch_size, to size the eval progress bar.
        return self.epoch_batches() * self.batch_size


def _cycle(n: int, rng: np.random.Generator):
    """Indices 0..n-1 in a fresh random order on every pass, forever."""
    while True:
        yield from rng.permutation(n)


def _pad(rows: list[SftExample], max_seq_length: int) -> tuple[mx.array, mx.array]:
    """Right-pad to a multiple of 32 like mlx_lm's iterate_batches: (tokens, loss mask)."""
    rows = [(toks[:max_seq_length], mask[:max_seq_length]) for toks, mask in rows]
    longest = max(len(t) for t, _ in rows)
    width = min(1 + 32 * ((longest + 31) // 32), max_seq_length)
    tokens = np.zeros((len(rows), width), np.int32)
    weights = np.zeros((len(rows), width), bool)
    for i, (toks, mask) in enumerate(rows):
        tokens[i, : len(toks)] = toks
        weights[i, : len(mask)] = mask
    return mx.array(tokens), mx.array(weights)


def iterate_mixed_batches(dataset: MixedDataset, batch_size, max_seq_length, loop=False, seed=None, comm_group=None):
    """Yields (tokens, loss mask, ref) with rows [SFT; chosen; rejected].

    Each pool is cycled independently, so a handful of edits keeps showing up
    next to a large replay pool. Without `loop`, stops after one epoch of the
    largest stream (what mlx_lm's `evaluate` expects).
    """
    rng = np.random.default_rng(dataset.seed if seed is None else seed)
    sft_iters = [(pool, k, _cycle(len(pool), rng)) for pool, k in dataset.sft]
    dpo_iter = _cycle(len(dataset.dpo), rng) if dataset.dpo else None
    while True:
        for _ in range(dataset.epoch_batches()):
            rows = [pool[next(it)] for pool, k, it in sft_iters for _ in range(k)]
            ref = []
            if dpo_iter is not None:
                picks = [next(dpo_iter) for _ in range(dataset.dpo_batch)]
                rows += [dataset.dpo[i][0] for i in picks]
                rows += [dataset.dpo[i][1] for i in picks]
                ref = [dataset.ref[i] for i in picks]
            tokens, weights = _pad(rows, max_seq_length)
            yield tokens, weights, mx.array(ref, dtype=mx.float32).reshape(-1, 2)
        if not loop:
            break


def token_logps(model, tokens: mx.array, weights: mx.array) -> tuple[mx.array, mx.array]:
    """Per-token log p(target) in float32, and which targets carry loss."""
    inputs, targets = tokens[:, :-1], tokens[:, 1:]
    logits = model(inputs)
    mask = weights[:, 1:]
    # The fused kernel accumulates in float32 and returns float32; keep it, since
    # DPO compares sums of these over whole replies.
    return -mx.fast.cross_entropy(logits, targets), mask


def mixed_loss(model, tokens, weights, ref, beta: float = 0.1, sft_weight: float = 1.0, dpo_weight: float = 1.0):
    logps, mask = token_logps(model, tokens, weights)
    n_dpo = ref.shape[0]
    n_sft = tokens.shape[0] - 2 * n_dpo
    ntoks = mask.sum()
    loss = mx.array(0.0)
    # Shapes are static under mx.compile, so these branches are fine.
    if n_sft:
        m = mask[:n_sft]
        # Without DPO rows `m` is all of `mask`. mx.compile fails ("key not found")
        # on a second, identical sum when one of them is an output, so reuse it.
        n = ntoks if n_dpo == 0 else m.sum()
        loss = loss + sft_weight * -(logps[:n_sft] * m).sum() / n
    if n_dpo:
        seq = (logps[n_sft:] * mask[n_sft:]).sum(axis=-1)
        chosen, rejected = seq[:n_dpo], seq[n_dpo:]
        margin = beta * ((chosen - ref[:, 0]) - (rejected - ref[:, 1]))
        loss = loss + dpo_weight * -nn.log_sigmoid(margin).mean()
    return loss, ntoks


def preference_logps(model, examples: list[DpoExample], max_seq_length: int, batch_size: int = 4) -> list[tuple[float, float]]:
    """Summed completion log-probs of (chosen, rejected) for each example."""
    model.eval()
    rows = [row for pair in examples for row in pair]
    sums = []
    for i in range(0, len(rows), batch_size):
        tokens, weights = _pad(rows[i : i + batch_size], max_seq_length)
        logps, mask = token_logps(model, tokens, weights)
        s = (logps * mask).sum(axis=-1)
        mx.eval(s)
        sums += s.tolist()
    return list(zip(sums[0::2], sums[1::2]))


def preference_report(model, examples: list[DpoExample], ref: list[tuple[float, float]], max_seq_length: int, beta: float) -> str:
    """How often the policy now prefers the user's final over the draft, relative to the reference."""
    pol = np.array(preference_logps(model, examples, max_seq_length))
    ref = np.array(ref)
    margins = beta * ((pol[:, 0] - ref[:, 0]) - (pol[:, 1] - ref[:, 1]))
    return (
        f"reward acc {np.mean(margins > 0):.2f}, margin {margins.mean():+.3f}, "
        f"Δlogp chosen {np.mean(pol[:, 0] - ref[:, 0]):+.2f} / rejected {np.mean(pol[:, 1] - ref[:, 1]):+.2f}"
    )
