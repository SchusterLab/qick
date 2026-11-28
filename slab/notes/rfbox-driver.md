# rfbox: the QICK box front-end driver

`qick_lib/qick/rfbox.py` drives the daughter cards of a ZCU216 QICK box (RF In,
RF Out, DC In, DC Out; balun cards are recognised and have nothing to drive).
`qick_lib/qick/rfbox_fake.py` is a fake box behind objects shaped like the
library's. Neither has been run on a board. The loopback checklist at the end
lists what has to be confirmed on hardware.

Deployment:

- one file, Python 3.8, standard library only. It imports nothing from
  `qick`, so a copy next to a server script works as `import rfbox`. On a PC
  without pynq it imports, and the validation, filter table, description
  format and fake all work;
- it runs inside whatever process holds the soc: the stock QICK Pyro server
  (qick 0.2.360 or 0.2.450, rfbv2 image), the lab board server, or a Jupyter
  kernel. It uses only the objects `RFQickSoc216V1` has had since 0.2.360
  (`adc_cards`, `dac_cards`, chains, chips, `board_sel`, `biases`, `soc.rf`)
  and never calls `rfb_*` methods (see the hazards below). Anything missing on
  an older library shows up as a value with source `unknown`/`error` and the
  reason, not as an exception;
- the board server (`qick.boardserver.rf`, `backend.py`) is a thin user of it:
  it keeps the `rf_get` / `rf_set` / `rf_clear_flags` wire records and turns
  rfbox errors into protocol errors. There is one code path.

## API

```python
from qick import rfbox            # or: import rfbox
box = rfbox.attach(soc)           # one RfBox per soc object, cached on it
```

| Call | What it does |
|---|---|
| `box.identity` | `{id, created, pid, soc, rfbox_api, rfbox_version, rfbox_sha256, qick_version}`. `id` is new for every soc object, so it changes on every server restart and image load. `rfbox_version` is `RFBOX_VERSION` ("0.2"); `rfbox_sha256` is the sha256 of the rfbox.py file as loaded, computed once at import (None if the file cannot be read). `get()` carries both at top level as well |
| `rfbox.version_info()`, mixin `rfbox_version()` | `{rfbox_version, rfbox_sha256, rfbox_api, file}` |
| `box.filter_probe(side, port, deselected=False)` | Raw reads of every ADMV8818 register of one RF In (`'adc'`) or RF Out (`'dac'`) port, each with its value or read error, plus the card select (board id, the AXI GPIO value 0x8 + id) and the chip select (`ch_en`, `cs_t`) used, the PRODUCT_ID and a verdict. Reads only. `deselected=True` also reads with no card selected |
| `with rfbox.locked(soc):` | The rfbox lock of that soc (re-entrant, the same object as `box.lock`), for code outside rfbox that drives the cards |
| `box.forget(why)` | Mark everything rfbox remembers writing (attenuators, filters, bias switch) unknown |
| `box.get(flags=True)` | Full state as plain JSON data. Reading never clears a flag |
| `box.layout(read_ids=True)` | Cards per slot (kind, library class, ID read now, ports), generator / readout / ADC block → port, slot, card channel, rail |
| `box.set(items, settle_s=0.2, protect=True)` | Validate everything, then apply in the safe order with readback. Afterwards clear the ADC flags, wait, and read them |
| `box.amp(ro, on, att=None)`, `box.ro_off(ro)` | One readout's amplifier, as its own item |
| `box.apply(description, others='leave'\|'safe')` | Declarative, in qick_rf's format and order (below) |
| `box.check(description, record)` / `check_applied` | Every reason the board no longer holds a description, including a new identity |
| `box.apply_safe(items=None)`, `box.before_load()` | Safe state; all amplifiers off before a load |
| `box.clear_flags(settle_s=0, blocks=None)`, `box.flags()` | Read and clear / only read the ADC interrupt words. `blocks` is a list of ADC block names (default: every block of the firmware); the record has `cleared`, `not_cleared` (read failed) and `blocks` |
| `box.set_limits(table)`, `box.limits` | The limits table every later `set` / `apply` is checked against |
| `rfbox.soc_class(RFQickSoc216V1)` | Served soc class with `rfbox_*` and `labdev_*` methods (plain data in and out, safe under Pyro's pickle serializer) |
| `rfbox.Local(soc)` | The same methods on a soc in a notebook |
| `rfbox.check_reply(reply)` | Client side: raises `Refused` again from a refusal reply |

A Pyro client may not have rfbox installed, so it cannot be relied on to
unpickle custom exceptions. The mixin therefore uses builtin exceptions only.
Which calls return a refusal as data and which raise:

| Call | Success | Refusal (`Refused`) | Bad request |
|---|---|---|---|
| `rfbox_set`, `rfbox_apply`, `rfbox_apply_safe` | the record | **returned as data**: `{'ok': False, 'refused': msg, 'code', 'record'}` (truthy: test `ok`, or pass it through `check_reply`, which raises `Refused`) | `ValueError('<code>: ...')` |
| `rfbox_get`, `rfbox_layout`, `rfbox_identity`, `rfbox_version`, `rfbox_clear_flags`, `rfbox_limits`, `rfbox_filter_probe` | the data | `RuntimeError('<code>: ...')` | `ValueError` |
| `rfbox_check` | a list of reasons; **empty means the board holds the description** | `RuntimeError` | `ValueError` |
| `labdev_adc_dsa`, `labdev_adc_flags`, `labdev_ro_off`, `labdev_filter_regs` | the value (`labdev_ro_off`: `True`) | `RuntimeError('<code>: ...')` (since 0.2; 0.1 returned the refusal dict, which is truthy) | `ValueError` |

In-process, the module helpers `adc_dsa`, `adc_flags`, `ro_off` and
`filter_registers` raise `Refused` or `RfboxError` and never return a
failure. `RfBox.set`, `apply` and `apply_safe` raise `Refused`, with the record
attached. Only the three `rfbox_*` record calls above turn a refusal into data.

`adc_dsa(soc, block, att=None, record=False)` (and `labdev_adc_dsa`) takes any
number from 0 to 27 dB and rounds it to the chip's 1 dB step with `round()`, as
qick_rf did. It returns the value read back, or with `record=True`
`{block, requested, applied, read, step_db, note}`. `set` items and
descriptions still require whole dB for `adc_att`, so a request there is never
silently changed.

### Values and their sources

Every hardware value in `get()` is `{'value', 'source', ...}`:

- `read`: read back from the chip just now (`raw` holds the register);
- `written`: the last value this driver wrote since the soc was constructed.
  The chip cannot be read; `by` says who wrote it;
- `assumed`: what the library constructor is believed to have written. Not
  confirmed;
- `unknown`: nothing is known (`why` says why);
- `error`: the read failed (`error` holds the exception).

```python
>>> st = box.get()
>>> st['adc_ports']['4']['att']
{'value': 31.75, 'source': 'assumed', 'by': 'library constructor', 'why': '...'}
>>> st['adc_ports']['4']['amp']
{'value': False, 'source': 'read', 'raw': 32, 'note': 'the expander pin (commanded state), not the rail'}
>>> st['dac_ports']['0']['supply']['shared_with']
[2]
```

### Settings

```python
box.set([{'ro': 0, 'adc_att': 27},                      # RF-ADC step attenuator, 0-27 dB, 1 dB
         {'ro': 0, 'att': 10, 'amp': True},             # RF In attenuator 0-31.75 dB in 0.25 dB; amplifier
         {'ro': 0, 'filter': {'ftype': 'bandpass', 'fc': 6.0, 'bw': 1.0}},   # resolved by FILTER_RULE
         {'gen': 1, 'filter': {'hpf': 26, 'lpf': None}},  # explicit steps (canonical; None = bypass)
         {'gen': 0, 'att': [6, 8], 'filter': {'ftype': 'lowpass', 'fc': 8.0}},  # RF Out att1, att2 (+ rail on)
         {'gen': 2, 'dc': True},                        # DC Out power
         {'ro': 1, 'gain': 10, 'amp': True},            # DC In gain -6..26 dB, powered up
         {'adc': '21', 'adc_att': 27},                  # an ADC block with no readout
         {'bias': 3, 'volts': 0.5}])                    # DAC11001, -10..10 V
```

Channels are addressed only by generator, readout, ADC block, port or bias
index. There are no channel names and no calibration.

- **Validation first.** Every field, then the board (card kind per slot), then
  the limits table, all before any chip is touched. `RfboxError` has `code`
  (`bad_request`, `no_rf_board`, `rf_limit`, `unknown_state`) and a key path. A
  wrong card names its slot and card, for example `readout 0 is on ADC port 4
  (ADC slot 2): DC In card, not RF In`.
- **Quantisation.** Each step records `requested` and `applied` (attenuators
  to 0.25 dB, filters by the deterministic rule below, bias to the 20-bit LSB) and
  `verified` (True / False / None when it cannot be read).
- **Order.** Phase `reduce` first: attenuation increases, amplifiers off and
  rails off, with the RF-ADC attenuator before the RF In attenuator. Then
  `filters` (readout filters before generator filters). Then `increase`:
  generator attenuator decreases, rails on, amplifiers on, DC In gain, and
  attenuation decreases with the RF-ADC attenuator last. Then `bias`. A step
  whose current value is unknown goes in `reduce`, so the transient is never
  worse than the final state. `amp: true` on an RF In readout is refused
  (`unknown_state`) unless its attenuator was written by this driver or is in
  the same request.
- **Failure midway.** `Refused` with `code` `step_failed` (an exception) or
  `readback` (a read disagreed). `record['order']` holds the steps done and
  `record['state']` is the full state re-read after the failure.
- **Flags at idle.** After the steps the ADC flags are cleared, `settle_s` is
  waited, and the flags are read. With `protect=True`, an over-voltage ADC has
  its amplifier switched off (`record['actions']`). `set` raises `Refused`
  (`adc_flags`) when a flag is set.
- **Shared rails.** Every RF Out rail change lists the paired channel in
  `affects` with its state before and after.

### Filters that do not read back

By default a filter write is verified: WR0_SW and WR0_FILTER are read back,
and if they differ from what was written or cannot be read, `set` and `apply`
stop at that step with `Refused('readback')`. In `apply`, that is before the
readout's amplifier is switched on. On a box whose ADMV8818s do not answer,
that refuses every application.

The opt-out is per channel: `filter_verify: false` next to the filter, in a
`set` item (`{'ro': 0, 'filter': {...}, 'filter_verify': False}`) or in a
description channel (`ros.R` or `gens.G`). With it:

- the write stands and the sequence goes on;
- the step has `verified: false`, `readback` (the registers read) or
  `readback_error`, and a `warning` that is also collected into the record's
  `warnings`. A read of all 0x00 or all 0xFF is called out as "the chip does
  not seem to answer";
- `get()` reports that filter as
  `{'source': 'written', 'verified': False, 'readback': 'differs'|'failed', 'regs_written', 'raw', ...}`,
  with the value decoded from what was written;
- `check()` compares the description with what rfbox wrote, not with the
  registers.

`filter_verify: true` is the default and is not stored, so descriptions
without the key keep their hash. `get()` never raises on a filter read: a
failure is `source: 'error'` (nothing written by rfbox) or the
written-not-verified record above.

For diagnosis, `filter_probe('adc', 4)` reads ADI_SPI_CONFIG_A, CHIPTYPE,
PRODUCT_ID_L/H, WR0_SW and WR0_FILTER one by one and says which card select
and chip select it used.

### Deterministic filter settings (0.3)

A filter setting is resolved by rfbox alone and gives the same registers on
every qick version.

**The explicit form is canonical.** `{'hpf': 26, 'lpf': None}` names each
section's step directly: step 0-63 = (band − 1) × 16 + state, with bands 1-4,
the numbering of the library's `set_filter_raw`. `None` (JSON `null`, or −1)
is bypass, and a missing key is bypass. It is accepted in `set` items and in
descriptions. It is also what records and `get()` report
(`value.explicit`, `written.explicit`, `resolved.explicit`), so it can be
copied back to reproduce a chip state exactly.

**Frequency requests** (`{ftype, fc, bw}` in GHz) are still accepted. They are
resolved by one rule, `FILTER_RULE = 'rfbox-never-narrower-1'`:

- The edges are: low-pass, upper edge = fc; high-pass, lower edge = fc;
  band-pass, lower edge = fc − bw/2 and upper edge = fc + bw/2; bypass, none.
  Each edge sets only its own section, so a high-pass request leaves the
  low-pass section in bypass, and vice versa.
- **High-pass section:** of the steps whose nominal corner is at or below the
  lower edge, the highest.
- **Low-pass section:** of the steps whose nominal corner is at or above the
  upper edge, the lowest.

The passband is therefore never narrower than asked. That is the one rule a
user can rely on without looking at the table, because an edge never moves into
the band being measured. The cost is that the edge moves outwards by up to one
step: 120-493 MHz depending on band.

**Band overlaps.** The four bands of each section overlap, and the same
corner is available in two bands. rfbox always prefers the **lower** band, as
far as the corner stays monotone in the request:

- low-pass band *b* serves every edge up to its state-15 corner (3.85, 7.25,
  13.0 GHz), and the next band takes over above that;
- high-pass band *b+1* takes over at its highest corner inside band *b*
  (3.40, 7.00, 12.50 GHz, `_HPF_SWITCH`).

Every resolution is therefore monotone in the request, and the band changes
only at those six fixed frequencies.

Whether the lower band is also the lower-loss one is **not established**. The
table has no loss figures. The users' field result (a band change costs about
1 dB in band) says the choice matters, not which way. The rule guarantees the
choice is consistent, and the explicit form lets a setup that has measured both
bands pin the better one.

**Out of range.** A high-pass edge below 1.75 GHz, or a low-pass edge above
18.85 GHz, bypasses that section, with a warning.

**Steps the rule never picks.** Some overlap steps are never picked by a
frequency request: HPF 14, 15, 31, 32, 47 and LPF 16, 17, 32, 48, 49. They are
reachable only in the explicit form.

**What the record shows.** Every filter step has:

- `requested`;
- `resolved`: `explicit`; `hpf` / `lpf` each with step, band, state and
  nominal GHz; the edges; the registers; `rule`; `table`; and `warnings`;
- `written`: the registers, band and state, nominal corners, `explicit` and
  `table`.

There are two warnings, both collected into `record['warnings']`:

- the edge fell between steps by more than half a step ("fell between steps:
  the corner used is ..., N MHz away");
- a request one step lower or higher would use a different band ("within one
  step of a band boundary").

**Pure helpers.** These need no hardware or pynq, and are also on the mixin as
`rfbox_filter_resolve` / `rfbox_filter_candidates`:

- `filter_resolve(f)` or `filter_resolve(type, fc, bw)`: exactly what `set` /
  `apply` write;
- `filter_candidates('hpf'|'lpf'|type, fc, bw=None, n=3)`: the `n` steps below
  and above each edge, across all bands, with nominal corners, offsets and the
  one the rule picks;
- `canonical_filter(f)`: the explicit form.

**Examples.**

| Request | Explicit form written | Nominal corners | Library nearest (0.2.449+) |
|---|---|---|---|
| high-pass fc 5.9 | `{'hpf': 25, 'lpf': None}` | HPF 5.71 GHz (warning: 190 MHz below the edge) | HPF 26 = 5.97 GHz |
| `{'hpf': 26, 'lpf': None}` | `{'hpf': 26, 'lpf': None}` | HPF 5.97 GHz | — |
| band-pass 6.6 / 1.4 | `{'hpf': 25, 'lpf': 33}` | 5.71 / 7.40 GHz (warning: next to a low-pass band boundary) | HPF 26, LPF 31 (7.25 GHz, band 2) |
| band-pass 6.65 / 1.5 | `{'hpf': 25, 'lpf': 33}` | 5.71 / 7.40 GHz | HPF 26, LPF 33 (7.40 GHz, band 3) |
| band-pass 6.7 / 1.6 | `{'hpf': 25, 'lpf': 34}` | 5.71 / 7.80 GHz | HPF 26, LPF 33 |
| low-pass fc 8.0 | `{'hpf': None, 'lpf': 35}` | LPF 8.20 GHz | LPF 35 |

The setting in use on the users' box is high-pass fc 5.9, which reads back
5.97 GHz. That is step 26, chosen by the nearest-step rule of rfbox 0.1/0.2.
**Under 0.3 the frequency form `{ftype: highpass, fc: 5.9}` resolves to step 25
(5.71 GHz) and writes a different state.** To keep today's chip state, store
`{'hpf': 26, 'lpf': None}`.

**Canonical descriptions and the hash.** A client can store the explicit form
and get the same chip state forever:

- `canonical_description(description)` and mixin `rfbox_canonical` return the
  description with every frequency-form filter replaced by its explicit steps;
- `apply` always returns it as `record['canonical']`;
- `spec_hash` is now computed on the canonical form and is versioned
  `'h2:' + 12 hex`. Two descriptions whose filters resolve to the same steps
  hash equal; for example 6.6/1.4 and 6.65/1.5 above both give
  `{'hpf': 25, 'lpf': 33}`.

**Compatibility.** Every description hash changes: `spec_hash` now returns
`h2:...` where 0.1/0.2 returned the bare 12-hex qick_rf hash. That old hash is
kept as `spec_hash_v1` and is in `apply` records as `hash_v1`.
`hash_matches(spec, h)` accepts either version, chosen by the prefix.
Descriptions that use `{hpf, lpf}` were refused by 0.1/0.2, so no stored
description changes meaning.

**The table** is `FILTER_BANDS`, version `FILTER_TABLE_VERSION =
'admv8818-nominal-1'`:

- the state-0 and state-15 corner of each band from the ADMV8818 datasheet, as
  qick 0.2.4xx `FilterADMV8818.freq2steps` lists them, with the 16 states evenly
  spaced, as the library assumes. The source is in `FILTER_TABLE_SOURCE`;
- the table version is in every filter record and in `get()`'s filter value;
- the driver applies no per-card measured correction. Calibration belongs in
  the user's platform; the explicit form is what to calibrate against.

**Mapping to the registers.** rfbox writes WR0_SW / WR0_FILTER itself
(`0xC0 | hpf_band << 3 | lpf_band`, `hpf_state << 4 | lpf_state`, band 0 =
bypass). This is the mapping of `set_filter_raw`, checked against the library
code. The `rfb_set_*_filter` calls of older libraries choose states with other
code, so their results can differ from rfbox's for the same fc / bw:

- 0.2.360 uses `_freq2band` / `_freq2bits` with span/16 steps;
- 0.2.449+ uses nearest-step;
- **qick 0.2.432 to 0.2.447 inclusive write bypass for every frequency-form
  request between 2 and 18 GHz**, because `FilterADMV8818._freq2step` matches
  the table's `bypass` entry first. 0.2.431 and 0.2.449+ are not affected, and
  raw-step calls (`set_filter_raw`, `rfb_set_*_filter_raw`) are unaffected.
  Found on a real box (filter registers left at bypass after a library
  band-pass call) and traced in the released PyPI packages, each version
  downloaded and its `rfboard.py` read: 0.2.431 fine, 0.2.432 first faulty,
  0.2.447 last faulty, 0.2.449 fine. The fault is in upstream's history too
  (`main` of this fork contains the 0.2.436 commit). A box on such a version that drives its filters through the
  library's frequency calls has every filter in bypass, which matches the
  users' "filters did not act". rfbox is unaffected because it writes the
  registers.

### Locking against the library's own methods

All of rfbox's hardware access holds one re-entrant lock per soc
(`box.lock`, `rfbox.locked(soc)`). The library's `rfb_*` methods and other
helpers do not take it. `soc_class(base)` (and `lock_rfb_methods(cls)`, which
the board server now applies to its `RFQickSoc216V1` class) wraps every
`rfb_*` method of the resulting class so that:

- it holds the lock;
- unless it is an `rfb_get_*` reader, everything rfbox remembers writing is
  afterwards marked `unknown`, with `why` naming the method and `previous` the
  old record. This matters for the attenuators, which cannot be read back.
  Readable values (DSA, expander pins, filters, gain, bias) are re-read by
  `get()` anyway.

The base class is not modified. Code that reaches the chips without an `rfb_*`
method (chain objects, `soc.rf`, other helpers) still bypasses the lock unless
it uses `with rfbox.locked(soc):`. Afterwards it should call
`attach(soc).forget('why')`. `prepare_round` / `cleanup_round` (which clear ADC
interrupts) are not wrapped.

### Limits table

```python
box.set_limits({'bias': {'3': [-1, 1]},          # with this section, unlisted outputs may only be 0 V
                'adc_att_min': {'20': 10},       # floor of the RF-ADC attenuator per block
                'ro_att_min': {'0': 6},          # floor of the RF In attenuator per readout
                'gen_att_min': {'0': [3, 3]},    # floor of the RF Out attenuators
                'dc_gain_max': {'1': 12}})       # ceiling of the DC In gain
```

### Safe state

`apply_safe(items=None)` does the following, in order:

1. every RF-ADC attenuator to 27 dB, read back, before anything else;
2. every readout amplifier off (RF In rail off and attenuator to 31.75 dB;
   DC In powered down), read back, unless `items` turns it on;
3. every RF Out attenuator to 31.75 dB;
4. `items` (an image's safe-state file) in the `set` order;
5. the idle flags.

Bias outputs are never stepped implicitly, and RF Out rails are left as they
are. `before_load()` switches every readout amplifier off and confirms it. It
raises `Refused` (`rf_safe_failed`) if it cannot.

### Declarative apply (replaces qick_rf.apply)

`normalize(description)` takes qick_rf's format with key-path messages:

- `adc_att` defaults to 27;
- non-zero bias volts require `limits`;
- `cards` gives the expected layout;
- readouts also take `amp: false`, and DC In readouts take
  `{gain, adc_att, amp}`;
- DC Out generators take `{dc}`.

`spec_hash` is the versioned `h2:` hash of the canonical form; `spec_hash_v1` gives the same hash as qick_rf
(see "Deterministic filter settings"). Filters may be given in the explicit form.

`apply` then does the following, in order:

1. Compares the layout with `cards`. Every channel must sit on a card of the
   right kind (`Refused('layout')`, nothing touched).
2. With `others='safe'`, switches off every amplifier not in the description.
3. Generators: filter, then attenuators and rail.
4. Each readout:
   - RF-ADC attenuator set and read back (`Refused('readback')` if it
     disagrees; the amplifier is not touched);
   - filter;
   - attenuator and amplifier on;
   - idle flags cleared and read twice, 0.2 s apart. Over-voltage at idle: the
     amplifier goes off again and `Refused('adc_flags')`. Over-range at idle:
     `Refused('adc_flags')` with the amplifier left on.
5. Bias, read back.

The record has `identity`, per-channel set/read values, `order`, `warnings`,
`changed` (every value that differs between before and after) and `state`.

## What can be read back, per chip

| Chip | Where | Readable? | How rfbox reads it |
|---|---|---|---|
| RF-ADC DSA (RFSoC Gen 3) | per ADC block | yes, integer dB | `RfBox._dsa_get` → `soc.rf.get_adc_attenuator` (qick.py `RFDC.get_adc_attenuator`; 0.2.360 indexes tiles by the name's digits) |
| ADC interrupt word | per ADC block | yes | `RfBox._flags_word` → `xrfdc._lib.XRFdc_GetIntrStatus(soc.rf._instance, ...)`; cleared only on request with `XRFdc_IntrClr` |
| MCP23S08 GPIO expander | one per card | yes: pins 0-3 outputs, 4-7 card ID | `RfBox._net_read` / `_amp_read` → `GpioMCP23S08.read_reg('GPIO_REG')` (drivers/hardware.py). The commanded pin level, not the rail |
| PE43705 attenuator | RF In (1/ch), RF Out (2/ch) | **no**, serial-in only | `written` / `assumed` / `unknown` from `RfBox._written` |
| ADMV8818 filter | RF In, RF Out | yes (SDO wired) | `RfBox._filter_read` → `FilterADMV8818.read_reg('WR0_SW'/'WR0_FILTER')`, decoded by `decode_filter` |
| LMH6401 gain | DC In | yes | `RfBox._gain_read` → `GainLMH6401.get_gain` (GAIN_REG) |
| DAC11001 bias | main board, 8 outputs | the setpoint register | `soc.biases[i].get_volt()` (`BiasDAC11001.get_volt`, DAC_DATA_REG) |
| bias output switch | AXI GPIO | not read | `written` / `assumed` (constructor) |
| card ID | expander pins 4-7 | yes | `RfBox._raw_card_id` → `filter_spi.send_receive_m` (also for slots the library left empty) |

## Hazards in the upstream library

All references are to the current tree (0.2.450) unless 0.2.360 is named.
rfbox avoids each of them.

| Where | What |
|---|---|
| `rfboard.py:481-485`, called at `:526` (RF Out) and `:559` (RF In) | `FilterChain.init_filter` opens and closes its own card selection inside the card constructor. From the second chain of a card onwards, the attenuator writes in the chain constructors (`AttenuatorPE43705.__init__`, `drivers/hardware.py:424` onwards) are made with the card deselected. Only chain 0 of each card is known at 31.75 dB after construction |
| `drivers/hardware.py:652-666` `SwitchControl.add_MCP` | The defaults are built MSB-first, so output *i* lands on pin 3−*i*, while `set_bits` uses pin *i*. DC In `PD0/PD1 = 1` is written to pins 3/2, so the real power-downs are written 0 (powered up) |
| `drivers/hardware.py:661` | `"GPIO net %s is already defined"` is raised without its argument |
| `rfboard.py:512` | RF Out rail `RFOUT5V0_EN((ch+1)%2)`: channels 0 and 2 share EN1, 1 and 3 share EN0. `enable_rf` (`:527-533`) switches the shared rail *before* writing the attenuators, and `disable` (`:535-541`) never switches it off |
| `rfboard.py:646-648` `DacRfCard216.disable_all` | Uses the RF In nets `RFIN5V0CH%d_EN`, which raises KeyError on an RF Out card |
| `rfboard.py:1178-1185` | Readout → card is `adc_cards[2*(tile-1) + block//2]`. Tile 0 gives a negative index (the wrong card, silently) and tile 3 an IndexError. rfbox maps from the ADC name and warns about tiles 0 and 3 |
| `rfboard.py:1260`, `:1299`, `:1359`, `:1373` | `rfb_set_ro_filter`, `rfb_set_gen_filter`, the raw filter setter and `rfb_set_rfadc_attenuator` all call `clear_interrupts()` on every ADC, so a filter change silently erases another readout's over-range record |
| `rfboard.py:1376-1381` | `prepare_round` / `cleanup_round` clear every ADC's interrupts around every acquisition |
| `rfboard.py:1188-1191` | Every ADC tile is restarted and every interrupt cleared at construction (0.2.4xx; not in 0.2.360) |
| `rfboard.py:1117-1121`, `drivers/hardware.py:359-362` | Every `BiasDAC11001` is written 0 V at construction, then `rfb_enable_bias`. **Every image load or server start steps all eight bias outputs to 0 V** |
| `drivers/hardware.py:1133` | LMH6401 written −6 dB at construction |
| `rfboard.py:1152-1161` | ID 0 is taken as DC In (an empty slot reading 0 would be one). Unknown IDs silently give `None`, and 0.2.360 does not recognise balun cards (IDs 5, 6) at all |
| `rfboard.py:688-725` `BoardSelection` | Global selection state with no lock anywhere in the library. Two threads using `rfb_*` interleave card selection |
| `drivers/hardware.py:746-763` `GpioMCP23S08.set_bits` | Read-modify-write from GPIO_REG. A failed or garbled read writes garbage to the other three pins |
| `drivers/hardware.py:316` `BiasAD5781.set_volt` | Unpacks `_volt2reg`'s single return value (ZCU111 boards) |
| `rfboard.py:1082`, `:1084` `rfb_get_lo` | Uses `.rfb`, which does not exist (ZCU111) |
| `qick.py:1712` | On Gen 3 the DSA is set automatically while an over-voltage interrupt is asserted. A DSA read during over-voltage may not be the programmed value |
| 0.2.360 `qick.py:594-628` | `RFDC.set_adc_attenuator` / `get_adc_attenuator` index tiles by the name's digits, and `set` returns None |
| released qick 0.2.432-0.2.447 (PyPI) `FilterADMV8818._freq2step` | Matches the table's `bypass` entry first: every frequency-form filter request between 2 and 18 GHz is written as bypass. 0.2.431 and 0.2.449+ are fine; raw-step calls are unaffected (reported by the measurement side, checked on the PyPI packages) |
| 0.2.360 `rfboard.py:868-977` | The older ADMV8818 band/state algorithm quantises differently and raises outside the bands. rfbox writes the registers directly with its own table (`FILTER_BANDS`, `filter_steps_for`), so both versions get the same state |

## What the constructor forces at load

`rfbox.LIBRARY_CONSTRUCTOR` holds this as data. The board server puts it in
every load record. Summary:

- **Bias**: all eight bias outputs to 0 V, and the bias switch enabled.
- **Card detection**: every slot is selected, IODIR = 0xf0, and the ID read.
- **GPIO defaults**: written mirrored (see `add_MCP` above). Measured on an
  rfbv2 box: an RF In amplifier left on by the previous image stays on.
- **Attenuators**: 31.75 dB on chain 0 of each card; the other chains are
  written while deselected.
- **Filters**: only `ADI_SPI_CONFIG_A`.
- **DC In gain**: −6 dB.
- **RF-ADC DSA**: not written. The bitstream load leaves it at 0 dB
  (measured), and at 0 dB an RF In amplifier alone drives the ADC over range.
- **0.2.4xx only**: tile restart and interrupt clear.

## Measured on hardware by the users

These were reported by the measurement side, measured through the older driver
(qick_rf / the library's `rfb_*` calls). They are not a validation of rfbox,
and the setup and its calibration are theirs.

- **RF Out attenuators:** the output attenuation was 4-7 % above the commanded
  dB at 6.9 GHz.
- **RF-ADC step attenuator:** its steps are about 0.4 dB larger than the
  nominal 1 dB.
- **RF In chain:** on that setup it compresses when the readout attenuator is
  set below 28 dB.
- **Filters, through the library:** they did not act on one box. That box
  runs qick 0.2.436, which writes bypass for every frequency request (see
  "Deterministic filter settings").
- **Filters, through rfbox 0.2 (rfbv2 box, through a fridge feedline):**
  - fc/bw requests that fall between table steps either land on the same state
    or jump to the next band, and a band jump changes the loss by about 1 dB.
    Band-pass 6.65/1.5 and 6.7/1.6 differ from 6.6/1.4 by 1 dB at 6.9 GHz;
    under the nearest-step rule the first two used low-pass band 3 and the
    third band 2;
  - the low-pass section costs 3-6 dB in band on both cards;
  - the setting now used is high-pass only, fc 5.9 GHz on both cards, which
    reads back 5.97 GHz (step 26).

## Loopback checklist (hardware)

Each line is: set, measure, confirm. Use a loopback from RF Out to RF In
through an external attenuator that keeps the ADC input safe, start with the
DSA at 27 dB, and keep amplifiers off unless the line says otherwise.

1. **Identity and layout.** Load the image twice. `layout()` slots and IDs
   should match the physical cards, and `identity.id` should differ between the
   loads. Read the ID of an empty slot (0 or 15?).
2. **DSA.** For 0, 10 and 27 dB, `set` the value and confirm that `get()`
   reads it back. With a tone in, the ADC level should change by the step.
3. **Amplifier across a load.** Turn an amplifier on, then load the image.
   Does `get()` read it on, and does the over-range flag latch at DSA 0? Then
   `apply_safe()` should give amp off and DSA 27, read back.
4. **GPIO defaults.** Right after a load, read DC In PD0/PD1 and the RF In
   rail pins. Do the constructor's defaults land at all?
5. **Deselected attenuator writes.** Right after a load, with RF Out
   channel 1 at a tone, write nothing and measure the output level. Then `set`
   31.75 dB. Is there a difference? If so, the constructor's write did not
   land.
6. **RF Out shared rail.** Set an attenuator on generator 0 (port 0) and
   check which other channel's output comes up (code: port 2). Repeat for
   port 1 (code: port 3).
7. **RF Out attenuators.** Set `att` 0/10/31.75 dB on each of the two stages.
   The output should change by the step.
8. **RF In attenuator.** With the amplifier on and DSA 27, set `att`
   31.75 → 20 → 10 dB. The ADC level should change by the step.
9. **Filters.** Set a low-pass well below the frequency of a tone: the tone should drop.
   Bypass should restore it, and `filter_registers` should read the same
   WR0_SW / WR0_FILTER and PRODUCT_ID 0x8818. If the registers read right
   but the filter does not act, check the chip's enable or power.
10. **DC In.** Set gain −6/10/26 dB: the level should change, `get()` should
    read the gain, and power-down should read back.
11. **Bias.** Set 0.5 V on output 3, measure it with a voltmeter, confirm the
    readback, then load the image. The output should be 0 V. Check whether
    the bias switch state can be read.
12. **Flags.** Overdrive deliberately at DSA 0. Over-range should be reported
    by `get()` and not cleared by it. `clear_flags` should clear it. Does the
    DSA read back the programmed value during over-voltage?
13. **Lock.** Run `get()` in a loop from two threads with one `set` loop.
    There should be no garbled IDs or GPIO values.

## Migration from qick_rf.py

| qick_rf.py | rfbox |
|---|---|
| `normalize(rf)` | `rfbox.normalize(rf)` (same format, same messages; adds `amp: false`, DC In and DC Out) |
| `plain(spec)`, `spec_hash(spec)` | `rfbox.plain`, `rfbox.spec_hash_v1` (the same hash); `rfbox.spec_hash` is the `h2:` hash of the canonical form (0.3), `rfbox.hash_matches` accepts either |
| `layout(cfg)`, `layout_text(lay)` | `rfbox.layout(cfg)` (= `cfg_layout`), `rfbox.layout_text`; live: `box.layout()` |
| `channels(cfg, lay)` | `rfbox.channels(cfg, lay)` |
| `problems(spec, cfg)` | `rfbox.problems(spec, cfg)`; live: `box.problems(spec)` |
| `flags(word)` | `rfbox.flags(word)` (= `decode_flags`, adds `word_hex`, `names`) |
| `has_helpers(soc)` | `rfbox.has_helpers(soc)` |
| `apply(soc, spec, cfg, log)` | `rfbox.attach(soc).apply(spec, log=log)` in-process; remote: `rfbox.check_reply(soc.rfbox_apply(spec))` |
| `check_applied(soc, spec, cfg)` | `attach(soc).check_applied(spec, record)` (or `soc.rfbox_check(spec, record)` for the full list) |
| `adc_dsa`, `adc_flags`, `ro_off` | `rfbox.adc_dsa`, `rfbox.adc_flags`, `rfbox.ro_off` (same signatures) |
| `filter_regs(soc, kind, ch)` | `rfbox.filter_registers(soc, kind, ch)` (`filter_regs` in rfbox is the register encoder) |
| `Local(soc)` | `rfbox.Local(soc)` (also has the `rfbox_*` methods) |
| `Refused` | `rfbox.Refused` (adds `code`, `record`) |
| `FTYPES`, `F_MIN`, `F_MAX`, `KINDS`, `NAMES`, `OVR_*`, `ATT_*`, `ADC_ATT_*`, `BIAS_MAX`, `IDLE_WAIT_S`, `FILTER_REGS` | same names in rfbox |
| `qick_pyro_server.py` `soc_class` with `labdev_*` | `rfbox.soc_class(RFQickSoc216V1)` (`labdev_adc_dsa`, `labdev_adc_flags`, `labdev_ro_off`, `labdev_filter_regs`, plus `rfbox_*`) |

## The fake

`qick.rfbox_fake.FakeQickBox(layout=None, threshold_db=10.0, gpio_defaults_land=False, library='0.2.450')`.
The interface is listed in its docstring and is kept stable, because other
repositories pin it.

- **Default population:** RF In in ADC slot 2 (readout 0 on port 4, block
  20); RF Out in DAC slots 0 and 1 (generators 0 and 1); DC Out in DAC slot 2.
- **`box.make_soc()`:** a load. It gives a new soc, sets the DSA to 0 and
  keeps the cards' state.
- **Chip selection:** chips ignore writes while their card is deselected.
- **Over-range physics:** an amplifier on with the DSA below `threshold_db`
  latches over-range. DSA 0 and attenuator 0 also latch over-voltage. The
  threshold is a model value.
- **Fault injection:** `box.fail(chip, ...)`, `box.stuck`, `box.inject`,
  `box.filter_mode[(side, port)] = 'zero' | 'ones' | 'raise'` (a filter chip
  that reads 0x00 or 0xFF, or whose read fails; writes still land).
- **Description lines:** card lines are added to `extra_description` only if
  the cfg does not already have them.
- **Assertions:** `box.select_conflicts` counts interleaved selections.
