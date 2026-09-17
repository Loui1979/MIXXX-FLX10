Created this repo for the Pioneer DDJ-FLX10 mappings and info for Mixxx.

MIDI mapping (ours): `Controllers/Pioneer-DDJ-FLX10.midi.xml` + `Pioneer-DDJ-FLX10-script.js`
HID / jog screens: separate controller in `HID/` (MIDI mapping untouched). See `HID/README.md`.

I did my own captures with rekordbox and decided to go full rekordbox, the displays are more luxurious.
After some successful testing, connecting and HID mode holding with my own daemon I will 
use Veezhuhz track waveform uploading scripts,  I am hoping it jives well with my handshake 
and my Pioneer_DDJ_FLX10_sripts.js
Much modding to do.  

This mapping was made with/for Mixxx 2.6+ (Lou-MX runs Mixxx 2.7.0 Flatpak nightly).

`MIXXX-FLX10 INDEX` — manuals, MIDI message list, spreadsheets.
`Scrap samples` — old code ideas.
`vendor/Veezuhz-Mixxx_FLX10` — upstream HID reverse-engineering snapshot.

## Credits

The HID handshake/unlock script and the jog-screen scripts are the work of
**Victor Pineda (Veezuhz)** — his reverse-engineering is what keeps the FLX10
alive on Mixxx. See [`CREDITS.md`](CREDITS.md). Upstream:
[Veezuhz/Mixxx_FLX10_Controller_Mapping](https://github.com/Veezuhz/Mixxx_FLX10_Controller_Mapping).
