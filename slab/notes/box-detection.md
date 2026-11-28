# QICK box detection: one firmware, one class

Dave's decision: the lab runs one ZCU216 firmware, the QICK-box (RF board v2) variant, on QICK boxes and on bare
ZCU216 eval boards, and one set of library classes that works out by itself whether it is in a box and which
daughter cards are in which slots. Branch `lab/box-detect` (on top of `lab/rf-driver`, rfbox 0.3).

Nothing here has run on a board. Everything below was checked only against the bus-level fake in
`qick/rfbox_fake.py` (`FakeRfBoardHW`), with the library's own card and chip drivers running over it
(`slab/tests/test_box_detect.py`).

## Use

    soc = QickSoc('qick_216_rfbv2.bit')      # in a box: a QickBoxSoc with the cards set up; else a plain QickSoc
    soc['rfbox']                             # inventory (below); printed in the description
    soc.rfbox                                # qick.rfbox.RfBox when a box is present, else None
    QickSoc(bitfile, rfbox=False)            # no detection: plain QickSoc even on the RF-board image
    QickSoc(bitfile, rfbox=True)             # raise NoBoxError if no box is found
    QickSoc(bitfile, rfbox={'cards': {'adc': {1: 'dc_in'}}})          # declare a card the probe cannot see
    QickSoc(bitfile, rfbox={'names': {'gate': ['rfb_gate_gpio'], 'gate_bit': 0,
                                      'presence': ['rfb_presence_gpio'], 'presence_channel': 1}})
    soc['fw_identity']                       # {'fw_id', 'fw_id_hex', 'build', 'matched_by', 'release', ...}

`RFQickSoc216V1` (`qick/rfboard.py`) is unchanged, so old scripts behave as before (including its unconditional
start-up writes).

## Entry point: QickSoc returns a subclass

`QickSoc.__new__` reads the `.hwh` next to the bitfile (an XML scan, no hardware). If every RF-board control IP is
there (`brd_sel_gpio`, `filter_spi`, `attn_spi`, `bias_spi`, `bias_gpio`, at any hierarchy depth) it constructs a
`qick.qickbox.QickBoxSoc` instead; otherwise, or with `rfbox=False`, or when called on a subclass, nothing changes.
`QickBoxSoc` is `RfBoxSocMixin + RFQickSoc216V1Mixin + QickSoc`, with every `rfb_*` method under the rfbox lock.

Why this rather than putting the detection inside `QickSoc.__init__`:

- users and servers keep writing `QickSoc(...)`; `isinstance(soc, QickSoc)` still holds;
- in a box the object has the whole `RFQickSoc216V1` API (`rfb_*`, `dac_cards`, `adc_chains`, `board_sel`, ...),
  so `qick.rfbox`, the board server and old notebook code work on it unchanged, and Pyro clients get the
  `rfbox_*` methods;
- on an eval image the returned object is exactly today's `QickSoc` class; the upstream file gets a small
  `__new__`, one ignored keyword (`rfbox`) and the identity block, so syncs stay easy;
- the class is chosen from the firmware (static); the behaviour from the hardware (detection at construction).

Without a box a `QickBoxSoc` behaves as a `QickSoc`: no card objects, `prepare_round` / `cleanup_round` are
QickSoc's (no ADC-interrupt refusal), `rfb_enable_bias` / `rfb_disable_bias` / `rfb_set_bias` / `rfb_get_bias`
raise `NoBoxError` (they would drive the bias switch or DACs), the port methods raise as for an empty slot, and the
ADC tiles are not restarted. Deviation from `RFQickSoc216V1` in a box: readouts on ADC tiles 0 and 3 are linked to
no card (the library links tile 0 to the wrong card and fails on tile 3).

## Decision order (`qick/rfbox_detect.py`, `detect`)

1. **No RF-board control IPs** in the firmware: inventory `reason: 'no rf-board control'`, nothing touched, nothing
   printed.
2. **Passive presence input** in the firmware (default name `rfb_presence_gpio`, channel 1, bit 0, active high; all
   configurable): read it with the gate closed and decide from it alone. Absent: no box, the gate stays closed and no
   SPI transaction is made. Present: open the gate and run the slot probe to list the cards (a box with every slot
   empty is still a box).
3. **Output gate but no presence input** (default `rfb_gate_gpio`, channel 1, bit 0 opens): set the idle levels
   first (card selection off, `filter_spi` and `bias_spi` chip selects high; AXI register writes, no clock), open
   the gate, probe; if no slot is confirmed, close the gate again: no box.
4. **No gate** (the stock rfbv2 images boxes run today): probe directly.

The gate, presence and `fw_id_gpio` names are placeholders for what the parallel firmware task builds; they are
looked up in `ip_dict` by last path component or full path, and can be set with `rfbox={'names': {...}}`
(`rfbox_detect.DEFAULT_NAMES` lists the keys).

## The slot probe and why it is safe

Per slot (board select = slot, plus 4 for ADC slots; the expander is chip select 4 on DAC cards, 2 on ADC cards,
as the library addresses it):

1. **Read** IODIR, IPOL, GPINTEN, DEFVAL, INTCON, IOCON, GPPU, GPIO, OLAT (9 transactions `41 rr 00`).
   - all 0x00 or all 0xFF: a constant line, nothing drives it. Verdict `empty`, stop. This is the case the library
     gets wrong: a line reading 0x00 gives `GPIO >> 4 = 0`, which `RFQickSoc216V1` takes for a DC In card.
2. **Read again with dummy byte 0xFF** (`41 rr ff`). The MCP23S08 ignores the byte it receives while it sends a
   register, so the two passes must agree. A line that follows the master (crosstalk from SDI or SCLK onto a
   floating SDO) or noise changes: verdict `unstable`.
3. **Signature** (Microchip MCP23S08 datasheet, DS21919, register reset values and IOCON layout): IODIR bits 7-4
   set (the ID pins are inputs: reset 0xFF, or 0xF0 after an earlier library start), IPOL = GPINTEN = INTCON = 0
   (reset values; no QICK code writes them), IOCON bits 7, 6, 0 clear (unimplemented, read 0). Failing it: `not_mcp`
   or `stuck`, listed with the values, not configured.
4. **Confirm by write-then-read of DEFVAL**: write 0xA5, read it back with both dummy bytes, the same with 0x5A,
   then write back the value read in step 1. DEFVAL is only the comparison value for interrupt-on-change, which is
   off (GPINTEN = 0 and INTCON = 0 were just read), so it has no external effect; it is the only register the probe
   writes (`Bus.mcp_write` refuses any other). A floating or echoing line cannot return a value the master is not
   sending at that moment. Failing it: `no_echo`.
5. **Card ID** = GPIO bits 7-4 from the first pass, mapped as the library does (DAC 1 DC Out, 3 RF Out, 6 Balun Out;
   ADC 0 DC In, 2 RF In, 5 Balun In). A confirmed expander with another ID (e.g. 15) is listed as `card` with
   `kind: null` and not configured.

On a confirmed RF card the ADMV8818s are then checked: read PRODUCT_ID (0x005, 0x004; expect 0x8818). If it does not
answer, write `ADI_SPI_CONFIG_A = 0x3C` (the same write the library's constructor makes; turns on the chip's SDO)
and read again. The result is recorded per chip in `chip_checks`; it does not decide presence.

What the probe never does before a box is confirmed: write IODIR, GPIO or OLAT (which would turn expander pins into
outputs and switch amplifier rails or power-downs), write an attenuator (`attn_spi`), a bias DAC (`bias_spi`) or the
bias switch (`bias_gpio`). Only after confirmation does `QickBoxSoc` run `RFQickSoc216V1`'s set-up, in its order and
with its objects, for the slots in the inventory: bias DACs (CONFIG1, 0 V), bias switch on, then per confirmed or
declared card the expander (IODIR 0xF0, defaults) and its chains (attenuators 31.75 dB, filter CONFIG_A, DC In gain).
The tests check this order on the fake's transaction log.

On a bare eval board the probe makes 72 SPI read transactions (8 slots × 9) on a constant line and 144 on a noisy
one, and no write at all.

Known limits:

- A box with no card that has an expander (all slots empty, or only cards without one) is reported as no box unless
  the firmware has a presence input. Whether balun cards have an MCP23S08 was not checked (IDs 5 and 6 suggest they
  do; the library comment "15 means empty or balun" suggests some did not).
- The first DC In version had a pinout fault that broke SPI reads (library comment). Such a card reads like an empty
  slot here; declare it: `rfbox={'cards': {'adc': {slot: 'dc_in'}}}`. Declaring any card asserts a box.
- If a box's empty-slot data line floats rather than being pulled, the verdict is `unstable` instead of `empty`;
  either way no card is configured.

## The eval-board pin question

`firmware/projects/qick_tprocv2_216_rfbv2_standard/ios.xdc` puts the RF-board control on the RFMC connectors'
digital I/O, LVCMOS18:

| Pins | Signals | Direction |
|---|---|---|
| ADCIO_00, 01, 03 | filter/expander SPI SCLK, SDI, CSn | out |
| ADCIO_02 | filter/expander SDO | in |
| ADCIO_06, 07, 10 | attenuator LE, SI, CLK | out |
| ADCIO_11-14 | card select enable, SEL0-2 | out |
| ADCIO_15 | bias switch DAC_BIAS_SWEN | out |
| ADCIO_09 | 2 MHz sync clock for the box's switching regulators | out, always toggling |
| ADCIO_08, DACIO_11-15 | spares | out |
| DACIO_00-07 | bias SPI (SCLK, SDIN, SDO in, CLRn constant 1, SYNCn, S0-S2) | out (SDO in) |
| DACIO_08-10 | card chip-select SEL0-2 | out |

On the eval kit these pins come out on header strips: the ZCU216 user guide (UG1390, appendix on the XM650/XM655
add-on cards, figure "High ADCIO and DACIO Digital I/O Header Pins") gives "a total of 20 DACIO and 20 ADCIO
digital I/O pins on the header strips", and the Xilinx wiki describes the XM650/XM655 headers as connecting "to IOs
on the PL side of the RFSoC as well as board power supplies". I could not read the pin-level figure, so **what else,
if anything, the XM655 connects to these pins is unknown here**. With nothing plugged into the headers, 1.8 V CMOS
outputs into open pins are harmless. The risk is a user's circuit or a jumper on those headers (the strips also
carry supply pins).

What software can change: with the stock rfbv2 image every one of these outputs is driven from the moment the
bitstream loads (the block design wires the SPI and GPIO outputs straight to the pins, with no tristate, and the
2 MHz clock and CLRn run without software). The probe only adds toggling of SCLK, SDI, CSn and the select lines on
pins that are already being driven. The firmware gate is what makes the image safe on an eval board with something
on the headers; until then, keep the eval board's ADCIO/DACIO headers unconnected when it runs the RF-board image.

## Inventory (`soc['rfbox']`)

Plain JSON data, the same on the board, over Pyro and in `QickConfig` on a PC (`soccfg['rfbox']`):

    {'version': 1, 'present': bool, 'reason': str, 'method': 'presence input' | 'gated probe' |
     'probe (no gate in this firmware)' | None,
     'gate': None | {'ip', 'open'}, 'presence_input': None | {'ip', 'word', 'present'},
     'cards': [{'slot', 'side': 'dac'|'adc', 'kind', 'name', 'id', 'channels', 'source': 'probe'|'forced',
                'chip_checks': [{'chip': 'MCP23S08', 'ok', 'why'}, {'chip': 'ADMV8818', 'cs', 'product_id',
                                'first_read', 'wrote_config_a'?, 'ok'}, ...]}],
     'slots': [{'side', 'slot', 'board_id', 'verdict', 'why', 'regs', 'id', 'confirmed', 'kind'?, 'echo'?}],
     'forced': {'adc 1': 'dc_in', ...}}

The description keeps `RFQickSoc216V1`'s slot lines ("ADC slot 2: RF In card has ports [4, 5] ..."), so
`qick.rfbox.cfg_layout` reads the layout from a saved configuration as before.

## Firmware identity (`soc['fw_identity']`)

At the end of every `QickSoc` construction: if the firmware has an AXI GPIO named `fw_id_gpio`, its channel 1 is
read as a 32-bit build ID; the build is looked up in the lab manifest (`qick.lab_release`) by that ID (entries with a
`fw_id` field; `lab_release.identify_fw_id`) or else by the sha256 of the `.hwh` / `.bit` (`identify`). Both are
optional and a failure only logs a warning. A description line appears only when something was found.

## Unverified on hardware, and a checklist for someone with a board

Unverified: every register value and timing above on real chips; the floating level of the SDO line on a bare eval
board and in a box's empty slot; that the ADMV8818 starts with SDO off; that balun cards carry an expander; the
gate and presence firmware (not built yet); the XM655 header question.

Bare ZCU216 eval board, stock rfbv2 image (nothing on the ADCIO/DACIO headers):

1. `soc = QickSoc(<rfbv2 bitfile>)`; `type(soc).__name__` is `QickBoxSoc`; `soc['rfbox']['present']` is False.
2. Record `[s['verdict'] for s in soc['rfbox']['slots']]` and one slot's `regs`: expected all `empty` (constant
   line) or `unstable`. Any `card` or `not_mcp` here is a finding: send the inventory.
3. `soc._rfbox_bus.log` holds every transaction: confirm only `mcp_read` entries.
4. Run a loopback program as on the eval image (it should behave as a plain QickSoc); `soc.rfb_enable_bias()` must
   raise `NoBoxError`.

QICK box, stock rfbv2 image:

1. Same construction; `present` True; the cards match the box's labels; every RF card's `chip_checks` show
   ADMV8818 `0x8818`. Note whether `first_read` already gave 0x8818 (SDO left on from before) or needed the
   CONFIG_A write.
2. Compare with `RFQickSoc216V1(<bitfile>)` on the same box: same `dac_cards` / `adc_cards` names and ports, same
   `soc.rfbox.get()` apart from identity fields.
3. Pull one card (power off first), reconstruct: that slot is `empty` (or `unstable`), not DC In.
4. A box with a first-version DC In card: it shows as empty; with `rfbox={'cards': {'adc': {slot: 'dc_in'}}}` it is
   set up.
5. Firmware with the gate (when built): on the eval board the gate ends closed and, with a presence input, the bus
   log is empty; in a box the gate is open and the cards are as above.
