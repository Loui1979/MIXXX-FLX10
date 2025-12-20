
// Pioneer-DDJ-FLX10-lights.js
// Handles lights, VU meters, and jog dial displays for Pioneer DDJ-FLX10

var DDJFLX10Lights = {};

// Constants for display states (from spreadsheet)
DDJFLX10Lights.timeModeState = [0x00, 0x00, 0x00, 0x00]; // 0x00: elapsed, 0x7F: remaining
DDJFLX10Lights.keyFormat = [0x00, 0x00, 0x00, 0x00]; // 0x00: classic, 0x7F: camelot
DDJFLX10Lights.jogDisplayState = [0x00, 0x00, 0x00, 0x00]; // 0x00: display, 0x7F: hide

DDJFLX10Lights.init = function() {
    // Connect engine for dynamic updates
    for (let i = 1; i <= 4; i++) {
        let group = `[Channel${i}]`;
        engine.connectControl(group, 'cue_point', 'DDJFLX10Lights.cuePointOutput');
        engine.connectControl(group, 'key', 'DDJFLX10Lights.keyOutput');
        engine.connectControl(group, 'playposition', 'DDJFLX10Lights.timeDisplayUpdate');
        engine.connectControl(group, 'VuMeter', 'DDJFLX10Lights.vuMeterOutput');
        engine.connectControl(group, 'play', 'DDJFLX10Lights.jogRingIllumination');
        // Add more light connections (e.g., playLed, cueLed) if moving all lights here
    }

    // Send initial MIDI for displays
    for (let i = 1; i <= 4; i++) {
        let deckNum = i - 1;
        midi.sendShortMsg(0x9F, 0x14 + deckNum, DDJFLX10Lights.timeModeState[deckNum]);
        midi.sendShortMsg(0xB0 + deckNum, 0x4A, DDJFLX10Lights.keyFormat[deckNum]);
        midi.sendShortMsg(0x9F, 0x5D + deckNum, DDJFLX10Lights.jogDisplayState[deckNum]);
    }
};

// Cue point output (MSB/LSB)
DDJFLX10Lights.cuePointOutput = function(value, group, control) {
    let deckNum = script.deckFromGroup(group) - 1;
    let msb = (value >> 7) & 0x7F;
    let lsb = value & 0x7F;
    if (value === -1) {
        msb = 0x7F;
        lsb = 0x7F;
    }
    midi.sendShortMsg(0xBF, 0x1C + deckNum, msb);
    midi.sendShortMsg(0xBF, 0x3C + deckNum, lsb);
};

// Key output (0-24 semitones)
DDJFLX10Lights.keyOutput = function(value, group, control) {
    let deckNum = script.deckFromGroup(group) - 1;
    midi.sendShortMsg(0xB0 + deckNum, 0x49, value & 0x18); // Mask to 0-24 as per spreadsheet
};

// Time display update (use playposition and duration to calc elapsed/remaining)
DDJFLX10Lights.timeDisplayUpdate = function(value, group, control) {
    let deckNum = script.deckFromGroup(group) - 1;
    let duration = engine.getValue(group, 'duration');
    if (duration > 0) {
        let timeSec = DDJFLX10Lights.timeModeState[deckNum] === 0x00 
            ? Math.floor(value * duration)  // Elapsed seconds
            : Math.floor((1 - value) * duration);  // Remaining seconds

        // Convert seconds to MM:SS format and send as MIDI (assuming controller expects 4 bytes: M tens, M units, S tens, S units)
        let minutes = Math.floor(timeSec / 60);
        let seconds = timeSec % 60;
        let timeBytes = [
            Math.floor(minutes / 10),  // Minutes tens
            minutes % 10,              // Minutes units
            Math.floor(seconds / 10),  // Seconds tens
            seconds % 10               // Seconds units
        ];

        // Send time bytes (adjust MIDI status/midino per actual; spreadsheet doesn't specify, so example using SysEx or CC - research needed)
        // Example: midi.sendSysexMsg([0xF0, 0x47, 0x00, deckNum + 1, 0x01] + timeBytes + [0xF7]); // Hypothetical SysEx
        // Or CC: for (let b = 0; b < 4; b++) midi.sendShortMsg(0xB0 + deckNum, 0x50 + b, timeBytes[b] * 8); // Example scaling to 0-127
    }
};

// VU meter output (scale 0-1 to 0-127)
DDJFLX10Lights.vuMeterOutput = function(value, group, control) {
    let deckNum = script.deckFromGroup(group) - 1;
    let vuVal = Math.round(value * 127);
    midi.sendShortMsg(0xBF, 0x40 + deckNum, vuVal);
};

// Jog ring illumination (on play)
DDJFLX10Lights.jogRingIllumination = function(value, group, control) {
    let deckNum = script.deckFromGroup(group) - 1;
    let illumVal = value > 0 ? 0x01 : 0x00; // WHITE=0x01 on play
    midi.sendShortMsg(0xBF, 0x09 + deckNum, illumVal);
};

// Toggle functions (call from main script on button press)
DDJFLX10Lights.toggleTimeMode = function(deckNum) {
    DDJFLX10Lights.timeModeState[deckNum - 1] = DDJFLX10Lights.timeModeState[deckNum - 1] === 0x00 ? 0x7F : 0x00;
    midi.sendShortMsg(0x9F, 0x14 + (deckNum - 1), DDJFLX10Lights.timeModeState[deckNum - 1]);
    // Trigger time update on toggle
    DDJFLX10Lights.timeDisplayUpdate(engine.getValue(`[Channel${deckNum}]`, 'playposition'), `[Channel${deckNum}]`, 'playposition');
};

DDJFLX10Lights.toggleKeyFormat = function(deckNum) {
    DDJFLX10Lights.keyFormat[deckNum - 1] = DDJFLX10Lights.keyFormat[deckNum - 1] === 0x00 ? 0x7F : 0x00;
    midi.sendShortMsg(0xB0 + (deckNum - 1), 0x4A, DDJFLX10Lights.keyFormat[deckNum - 1]);
};

DDJFLX10Lights.toggleJogDisplay = function(deckNum) {
    DDJFLX10Lights.jogDisplayState[deckNum - 1] = DDJFLX10Lights.jogDisplayState[deckNum - 1] === 0x00 ? 0x7F : 0x00;
    midi.sendShortMsg(0x9F, 0x5D + (deckNum - 1), DDJFLX10Lights.jogDisplayState[deckNum - 1]);
    // If shown, update time
    if (DDJFLX10Lights.jogDisplayState[deckNum - 1] === 0x00) {
        DDJFLX10Lights.timeDisplayUpdate(engine.getValue(`[Channel${deckNum}]`, 'playposition'), `[Channel${deckNum}]`, 'playposition');
    }
};

// Cue point output (MSB/LSB)
DDJFLX10Lights.cuePointOutput = function(value, group, control) {
    let deckNum = script.deckFromGroup(group) - 1;
    let msb = (value >> 7) & 0x7F;
    let lsb = value & 0x7F;
    if (value === -1) {
        msb = 0x7F;
        lsb = 0x7F;
    }
    midi.sendShortMsg(0xBF, 0x1C + deckNum, msb);
    midi.sendShortMsg(0xBF, 0x3C + deckNum, lsb);
};

// Key output (0-24 semitones)
DDJFLX10Lights.keyOutput = function(value, group, control) {
    let deckNum = script.deckFromGroup(group) - 1;
    midi.sendShortMsg(0xB0 + deckNum, 0x49, value & 0x18); // Mask to 0-24 as per spreadsheet
};

// Time display update (use playposition and duration to calc elapsed/remaining)
DDJFLX10Lights.timeDisplayUpdate = function(value, group, control) {
    let deckNum = script.deckFromGroup(group) - 1;
    let duration = engine.getValue(group, 'duration');
    if (duration > 0) {
        let timeVal = DDJFLX10Lights.timeModeState[deckNum] === 0x00 
            ? Math.floor(value * duration)  // Elapsed
            : Math.floor((1 - value) * duration);  // Remaining
        // Send as MIDI if needed, but spreadsheet uses static mode toggle; adjust if dynamic time send required
        // For now, assume mode toggle handles display, and this updates if controller expects time value (not in spreadsheet)
    }
};

