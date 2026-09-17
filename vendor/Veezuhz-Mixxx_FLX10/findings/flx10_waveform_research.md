# FLX10 / rekordbox / Serato Waveform Rendering Research
Version: 2026-05
Compiled for agent ingestion / engineering reference.

---

# DISCLAIMER

This document combines:
- publicly documented behavior,
- reverse-engineered rekordbox analysis formats,
- observed UI behavior,
- and engineering inference.

Not all details are officially documented by AlphaTheta/Pioneer or Serato.

---

# CONFIDENCE LEVELS

| Topic | Confidence |
|---|---|
| PWV5 = 150 entries/sec | HIGH |
| rekordbox uses time-domain waveform indexing | HIGH |
| FLX10 receives waveform buffers + transport updates instead of raw video | HIGH |
| PWV5 bitfield layout | HIGH |
| Serato uses beat-domain scaling/interpolation | MEDIUM |
| Exact FLX10 HID protocol | LOW |
| Exact FPS to FLX10 display | UNKNOWN |
| Serato internal waveform cache structure | LOW |

---

# SECTION 1 — FLX10 DISPLAY COMMUNICATION

No public SDK or protocol documentation exists describing:
- exact display FPS,
- exact USB packet structure,
- exact LCD refresh timing,
- or jog waveform transport protocol.

Observed behavior strongly suggests:
- waveform buffers are precomputed,
- transport state is streamed,
- rendering occurs locally on the controller.

Likely transmitted:
- playback position
- waveform buffers
- beat markers
- cue markers
- zoom state
- deck state

Likely NOT transmitted:
- raw video frames

---

# SECTION 2 — REKORDBOX PWV5 MODEL

rekordbox stores detailed waveform data inside PWV5 sections of ANLZ files.

Reverse-engineered documentation indicates:
- 75 frames/sec
- 2 waveform entries per frame

Effective density:

```math
150 waveform entries/sec
```

This appears deterministic and fixed-rate.

---

# SECTION 3 — CORE TIME MAPPING

## Playback Time → Waveform Index

```math
i = t * 150
```

Where:
- i = waveform index
- t = playback time in seconds

---

## Waveform Index → Playback Time

```math
t = i / 150
```

---

# SECTION 4 — TOTAL BUFFER SIZE

```math
N = T * 150
```

Where:
- N = total waveform entries
- T = total track duration in seconds

---

# SECTION 5 — CENTER-PLAYHEAD RENDER MODEL

Pioneer-style waveform rendering keeps:
- playhead fixed,
- waveform scrolling underneath.

Center index:

```math
i_c = p * 150
```

Where:
- i_c = center waveform index
- p = playback time

Visible window:

```math
i_start = i_c - W/2
```

Render range:

```text
[i_start ... i_start + W]
```

---

# SECTION 6 — PIXEL TRANSFORM

Waveform index → screen position:

```math
x = (i - i_c) * s
```

Where:
- x = pixel position
- s = pixels per waveform sample

---

# SECTION 7 — SMOOTH MOTION

Best practice:
DO NOT recompute waveform position from percentages every frame.

Use continuous accumulation:

```cpp
waveIndex += deltaTime * 150.0;
```

Benefits:
- smoother motion
- lower jitter
- reduced drift

---

# SECTION 8 — PWV5 DATA LAYOUT

PWV5 entries are packed 16-bit values.

Reverse-engineered layout:

```text
RRRGGGBBBHHHHHxx
```

Fields:
- R = red intensity (3 bits)
- G = green intensity (3 bits)
- B = blue intensity (3 bits)
- H = waveform height (5 bits)
- xx = unused

---

# SECTION 9 — PWV5 DECODING

```cpp
r = (v >> 13) & 0x7;
g = (v >> 10) & 0x7;
b = (v >> 7)  & 0x7;
h = (v >> 2)  & 0x1F;
```

Normalize:

```cpp
height = h / 31.0f;
```

---

# SECTION 10 — LIKELY FLX10 PIPELINE

Most likely architecture:

1. rekordbox computes waveform buffers
2. software sends waveform metadata + transport state
3. FLX10 firmware computes offsets locally
4. LCD render thread draws frames

This is consistent with:
- USB HID bandwidth limits
- observed smoothness
- Pioneer historical architecture

---

# SECTION 11 — ESTIMATED DISPLAY FPS

No confirmed value publicly exists.

Observed smoothness suggests:
- approximately 30–60 FPS effective rendering

This remains unverified.

---

# SECTION 12 — SERATO MODEL

Serato appears fundamentally different from rekordbox.

Observed behavior suggests:
- beat-domain rendering
- transient-anchor interpolation
- zoom-adaptive waveform density
- dynamic scaling during sync

Confidence: MEDIUM

---

# SECTION 13 — SERATO CORE MODEL

rekordbox-style:

```cpp
index = time * 150;
```

Serato-style appears closer to:

```cpp
beatPos = time * bpm / 60.0;
pixel   = beatPos * pixelsPerBeat;
```

---

# SECTION 14 — BEAT POSITION EQUATION

```math
b(t) = (t * BPM(t)) / 60
```

Where:
- b(t) = beat position
- BPM(t) = instantaneous BPM

---

# SECTION 15 — PIXELS PER BEAT

```math
x = b(t) * P_b
```

Where:
- P_b = pixels-per-beat scaling

---

# SECTION 16 — SERATO "STRETCHY WAVEFORMS"

Observed behavior suggests dynamic scaling:

```math
x' = x * (BPM_master / BPM_track)
```

Effects:
- synced visual beat alignment
- dynamic waveform scaling
- adaptive waveform spacing

---

# SECTION 17 — TRANSIENT ANCHOR THEORY

Possible internal structure:

```cpp
struct Anchor {
    float time;
    float beat;
    float pixel;
};
```

Interpolation:

```cpp
pixel = lerp(anchorA.pixel,
             anchorB.pixel,
             localBeatPhase);
```

---

# SECTION 18 — LIKELY SERATO COMPONENTS

Likely systems:
- transient anchors
- beatgrid anchors
- waveform mipmaps
- multi-resolution caches
- beat-domain interpolation

---

# SECTION 19 — KEY DIFFERENCE

## rekordbox

- absolute-time waveform indexing
- deterministic buffers
- fixed density

## Serato

- beat-domain rendering
- dynamic scaling
- transient-aware interpolation

---

# SECTION 20 — IMPLEMENTATION RECOMMENDATIONS

## rekordbox/CDJ-style renderer

Use:
- fixed 150 Hz buffers
- time-domain indexing
- center-playhead scrolling

Recommended:

```cpp
waveIndex += deltaTime * 150.0;
```

---

## Serato-style renderer

Use:
- beat-domain positioning
- transient anchors
- adaptive zoom density
- pixels-per-beat rendering

Recommended:

```cpp
beatPos = time * bpm / 60.0;
pixel   = beatPos * pixelsPerBeat;
```

---

# SECTION 21 — ENGINEERING NOTES

Recommended rendering strategy:
- waveform data fixed at source resolution
- interpolate motion visually
- render at monitor refresh rate
- GPU scrolling preferred
- avoid rebuilding geometry every frame

---

# SECTION 22 — OPEN QUESTIONS

Still publicly unknown:
- exact FLX10 HID packets
- exact LCD refresh timing
- exact FPS
- exact Serato cache format
- exact transport protocol

---

# SECTION 23 — SOURCES

## Primary Technical References

### pyrekordbox

Reverse-engineered rekordbox analysis formats.

Key contribution:
- PWV5 timing
- waveform density
- ANLZ structures

URL:
https://pyrekordbox.readthedocs.io/en/stable/formats/anlz.html

---

### Deep Symmetry DJ Link Analysis

Reverse-engineered Pioneer analysis documentation.

Key contribution:
- PWV5 bitfield structure
- waveform entry decoding
- ANLZ internals

URL:
https://djl-analysis.deepsymmetry.org/rekordbox-export-analysis/anlz.html

---

## Official Product Documentation

### AlphaTheta / Pioneer DJ — DDJ-FLX10

Key contribution:
- jog display capabilities
- 3-band waveform support
- rekordbox/Serato support

URL:
https://www.pioneerdj.com/en/product/controller/ddj-flx10/black/overview/

---

### VirtualDJ FLX10 Documentation

Key contribution:
- scratch waveform terminology
- jog display behavior

URL:
https://www.virtualdj.com/manuals/hardware/pioneer/ddjflx10/layout/decks.html

---

### Serato Beatgrid Documentation

Key contribution:
- beatgrid behavior
- BPM/beat-domain alignment concepts

URL:
https://support.serato.com/hc/en-us/articles/202523390-Introduction-to-Beatgrids

---

# SECTION 24 — OBSERVATIONAL SOURCES

The following were used only as behavioral references:
- Reddit discussions
- user reports
- reverse-engineering communities
- DJ software observations

These are NOT authoritative protocol specifications.

---

# FINAL CONCLUSION

Most likely realities:

## rekordbox / CDJ architecture

- deterministic time-domain waveform indexing
- fixed-rate waveform buffers
- local hardware rendering

## Serato architecture

- beat-domain interpolation
- transient-aware scaling
- dynamic sync-aligned rendering

This aligns with observed behavior during:
- scratching
- sync
- DVS
- tempo changes
- waveform zoom
- jog rendering