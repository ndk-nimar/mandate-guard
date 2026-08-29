"""The two endings, priced separately. **Pinterest, KDD 2018**, extended.

Pinterest's contribution was to price a notification against the *lifetime* value at
stake rather than against the click. The extension this project makes -- argued in
`docs/problem.md` §6.2 -- is that "the mandate ended" is not one outcome but two, and they
recover at different rates:

```
loss on lapse       =  L * (1 - q)
loss on revocation  =  L * (1 - r)  +  alpha * R
```

`q` is the chance of winning back a customer whose mandate expired quietly; `r` is the
chance of winning back one who cancelled in irritation. **`q > r` is enforced, not hoped
for**, in three places: here, in `models.Mandate`, and in `policy.loader` at config-load
time.

Two things break if the two collapse into one number.

**A failed ask stops being able to convert a soft ending into a hard one.** That
conversion is the entire reason contacting a probably-doomed mandate is not free. With one
shared recovery probability the model cannot express it, and the optimiser will happily
spray.

**`r = 0` is the opposite error.** Treating revocation as total loss makes the system
pathologically conservative -- it stops asking anyone, for a reason that is an artefact of
the parameterisation rather than a fact about customers.

T1.2 measured `q = 0.407` on real data and bounded `r` at 0.293 (`docs/mapping.md` §2).
The gap is therefore not a modelling assumption any more; it is a measurement on 1.57M and
772k independently-defined events.
"""

from __future__ import annotations

from mandateguard.value.reachability import reachability_loss_inr


def loss_on_lapse_inr(ltv_remaining_inr: float, recovery_after_lapse: float) -> float:
    """`L * (1 - q)` -- what a quiet ending costs, net of the chance of winning them back."""
    return ltv_remaining_inr * (1.0 - recovery_after_lapse)


def loss_on_revocation_inr(
    ltv_remaining_inr: float,
    recovery_after_revocation: float,
    reachability_value_inr: float,
    alpha: float,
) -> float:
    """`L * (1 - r) + alpha * R` -- strictly worse than lapsing, by construction.

    The second term is not a fudge factor for "revocation feels worse". It is a different
    asset: the *channel* to that customer, which has value across every future mandate and
    not only this one. See `reachability.py`.
    """
    return ltv_remaining_inr * (1.0 - recovery_after_revocation) + reachability_loss_inr(
        reachability_value_inr, alpha
    )
