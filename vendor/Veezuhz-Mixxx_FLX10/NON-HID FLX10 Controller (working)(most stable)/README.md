# Pioneer DDJ-FLX10 Mixxx Mapping

Mixxx controller mapping for the Pioneer DDJ-FLX10 (v1.0.1).


## Files explanation

There are 3 files that need to go into you Mixxx controller folder.
   * `Pioneer-DDJ-FLX10-scripts.js`
   * `Pioneer-DDJ-FLX10-midi.xml`
   * `common-controllelr-scripts.js`

**File:** `flx10_unlock_v2.py` is meant for linux users. 
I ran into an issue where the jogwheels would only show 'no audio driver'. I had to try and reverse engineer the handshake between pioneer settings utility and the flx10. The steps for this are:
   1. connect flx10
   2. run `pkill -f mixxx 2>/dev/null` --this is to close mixxx
   3. run  `sudo python3 ~/Downloads/flx10_unlock_v2.py` --running python script
   4. This should unlock your flx10 and you wont need to do this again until you unplug/plug back in (reconnect)

**Sidenotes:** yes... I'm sorry.. this has to be done every startup so keep note that this needs to be done with mixxx closed. I hope someone can create a working .sh for this.


## BeatFX Chain Preset Setup

The 14 BEAT FX selector positions on the FLX10 map directly to the first 14 chain presets in your Mixxx Preferences → Effects list, in order.

**Setup:**

1. Open Mixxx → Preferences → Effects.
2. Under "Effect Chain Presets", create or arrange 14 presets in the following order to match the controller's printed labels:

   | Position | Label         | Suggested chain                  |
   | -------- | ------------- | -------------------------------- |
   | 1        | Low Cut Echo  | HPF + Echo                       |
   | 2        | Echo          | Echo                             |
   | 3        | MT Delay      | Echo (long feedback)             |
   | 4        | Spiral        | Flanger (light)                  |
   | 5        | Reverb        | Reverb                           |
   | 6        | Trans         | LPF + Tremolo                    |
   | 7        | Enigma Jet    | Phaser + Flanger                 |
   | 8        | Flanger       | Flanger                          |
   | 9        | Phaser        | Phaser                           |
   | 10       | Stretch       | Reverb + Echo                    |
   | 11       | Slip Roll     | Echo + Flanger                   |
   | 12       | Roll          | Echo + Reverb + Flanger          |
   | 13       | Mobius        | Echo + Reverb + Phaser           |
   | 14       | Mobius (off)  | Empty chain (deactivates output) |

3. To save a chain as a preset: load effects into Effect Unit 1 via the GUI, set parameters, then click the chain's save icon and name it.
4. To reorder, drag entries in the preset list.
5. Restart of Mixxx is not required — changes take effect immediately.
