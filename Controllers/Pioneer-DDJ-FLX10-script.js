// Pioneer-DDJ-FLX10-script.js
// MIDI mapping for Pioneer DDJ-FLX10
// Mixxx version 2.6
// =============================================================================
// Artist: Lougazi(Sweet Lou)   
// Version: 3.1 (Reorganized)
// =============================================================================
// Supporting: 4 decks, jog wheels, pad modes, mixer controls
// Features: Pad lights, scratch, seek, jog displays
// Uses: Component JS framework
// =============================================================================
//  What's left? 
//  Stems
//  Loop controls,  
//  Some more controls I don't use often, but want to
//  Some Led's to get working
//  More pad modes(not sure how or what modes yet)  
//      Need to see what works bestwith mixxx, and what we want
//  Effects (Mixxx's effects system is different than Rekordbox)
//  Display tinkering(sync_leader, key, yada, yada)
//  And finally the almighty HID mode (we will get there)
//  
//  This version out for Christmas 2025,  full and HID available in the coming 
//      weeks.
//  I will be updating regularily, daily, weekly, as this will be my main 
//      Mixxxing software that I want too use.
//  Enjoy  ;)
//=============================================================================
//
// eslint-disable-next-line no-var
var DDJFLX10 = {};
//
// =============================================================================
// ===== USER CONFIGURABLE OPTIONS =====
// =============================================================================
DDJFLX10.USER_CONFIG = {
    // Jog wheel settings
    jogWheelSensitivity: 1.0,
    vinylMode: true,
    
    // Display settings
    ledBrightness: 1,
    enableVuMeters: true,
    enableJogTime: false,
    enableJogDisplay: false,   // HID test: ch16 MIDI Deck Info fuses the LCD
    enableJogRingFlash: true,
    // Re-enabled: Mixxx → controller MIDI (LEDs/VU) now relay-paired
    enableMidiOut: true,
    
    // Performance settings
    quickJumpSize: 16, // Beats for quick jump
    jogRingFlashIntervalMs: 300,
    
    // Debug settings
    debugPadInput: false,      // Log pad input events
    debugJogDisplay: false,    // Log jog display updates
    debugMidi: false,          // Log all MIDI messages
    // Log FLX10_TRACK_LOAD / FLX10_POS / FLX10_BPM for flx10_screen_daemon.py
    enableHidDaemonIpc: true,
    // Rekordbox SysEx so jog firmware accepts HID waveforms (not Serato)
    enableRekordboxSysex: true
};
// Mixxx 2.7: midi.sendShortMsg is read-only — do not assign to it.
// Chris: mute short MIDI-OUT for HID/rekordbox SysEx test; SysEx stays on.
DDJFLX10._outShort = function(status, data1, data2) {
    if (DDJFLX10.USER_CONFIG.enableMidiOut === false) {
        return;
    }
    midi.sendShortMsg(status, data1, data2);
};
// Jog wheel configuration (per Mixxx manual)
DDJFLX10.JOG_CONFIG = {
    RESOLUTION: 5760,        // Intervals per revolution for scratchEnable
    RPM: 33 + 1/3,           // 33⅓ rpm vinyl speed
    ALPHA: 1.0 / 32,         // Scratch filter (manual default)
    BETA: (1.0 / 32) / 64,   // Scratch acceleration filter
    BEND_SCALE: 0.025,       // Pitch bend sensitivity
    BEND_RESET_MS: 40        // Pitch bend reset timer
};
// =============================================================================
// ===== CONSTANTS & ENUMS =====
// =============================================================================

DDJFLX10.MIDI = {
    // Message types
    NOTE_ON: 0x90,
    NOTE_OFF: 0x80,
    CC: 0xB0,
    
    // Pad MIDI status bytes (Normal/Shifted for 4 decks)
    PAD_STATUS: [0x97, 0x98, 0x99, 0x9A, 0x9B, 0x9C, 0x9D, 0x9E],
    
    // Jog display (Channel 16)
    JOG_DISPLAY_CC: 0xBF,
    JOG_DISPLAY_NOTE: 0x9F,
    
    // LED states
    LED_OFF: 0x00,
    LED_ON: 0x7F
};

DDJFLX10.DECKS = {
    DECK_1: 1,
    DECK_2: 2,
    DECK_3: 3,
    DECK_4: 4,
    COUNT: 4
};

// =============================================================================
// ===== LOOKUP TABLES & ARRAYS =====
// =============================================================================
// Mixxx ChromaticKey 1-24 → Pioneer Sheet 6 Data2 0x01-0x18
DDJFLX10.PIONEER_KEY_FROM_MIXXX = {
    1: 0x01,  2: 0x03,  3: 0x05,  4: 0x07,  5: 0x09,  6: 0x0B,
    7: 0x0D,  8: 0x0F,  9: 0x11, 10: 0x13, 11: 0x15, 12: 0x17,
    13: 0x08, 14: 0x0A, 15: 0x0C, 16: 0x0E, 17: 0x10, 18: 0x12,
    19: 0x14, 20: 0x16, 21: 0x18, 22: 0x02, 23: 0x04, 24: 0x06
};

// Tempo ranges
DDJFLX10.TEMPO_RANGES = [0.06, 0.10, 0.16, 0.25];

// PadFX beat loop roll sizes (in beats) - Page 1 and Page 2
DDJFLX10.PADFX_SIZES = {
    // Page 1: Short loops (1/32 to 4 beats)
    1: [0.03125, 0.0625, 0.125, 0.25, 0.5, 1, 2, 4],
    // Page 2: Longer loops (1/16 to 32 beats)  
    2: [0.0625, 0.125, 0.25, 0.5, 1, 2, 4, 8]
};

// Beat Jump sizes (in beats) - for potential beat jump mode
DDJFLX10.BEATJUMP_SIZES = {
    1: [1, 2, 4, 8, 16, 32, 64, 128],
    2: [0.25, 0.5, 1, 2, 4, 8, 16, 32]
};

// Pad configuration
DDJFLX10.PAD_CONFIG = {
    MULTIPLIER: 0x08,
    COUNT: 16,          // Total pads (8 per page * 2 pages)
    PER_PAGE: 8
};

// Jog display controls (Channel 16 / CC 0xBF or Note 0x9F)
DDJFLX10.JOG_DISPLAY = {
    // Time display
    TIME_MIN: [0x42, 0x44, 0x46, 0x48],  // Deck 1-4
    TIME_SEC: [0x43, 0x45, 0x47, 0x49],  // Deck 1-4
    
    // Sheet 6 NOTE 0x9F
    MASTER: [0x18, 0x19, 0x1A, 0x1B],    // MASTER (Beat Sync)
    SYNC: [0x1C, 0x1D, 0x1E, 0x1F],      // SYNC (Beat Sync)
    KEYLOCK: [0x20, 0x21, 0x22, 0x23],   // Master tempo
    VISIBILITY: [0x5D, 0x5E, 0x5F, 0x60], // Display/hide jog info

    // Sheet 6 CC 0xBF
    RING: [0x09, 0x0A, 0x0B, 0x0C],      // Jog ring illumination
    CUE_MSB: [0x1C, 0x1D, 0x1E, 0x1F],
    CUE_LSB: [0x3C, 0x3D, 0x3E, 0x3F],

    // Sheet 6 deck-channel CC (Bn, not ch16)
    KEY: 0x49,
    KEY_CHANGE: 0x4A,
    KEY_FORMAT: 0x5B,

    // Jog display data (Sheet 6)
    MARKER_MSB: [0x10, 0x11, 0x12, 0x13], // Digital marker
    MARKER_LSB: [0x30, 0x31, 0x32, 0x33],
    BPM_MSB: [0x14, 0x15, 0x16, 0x17],     // BPM
    BPM_LSB: [0x34, 0x35, 0x36, 0x37],
    SPEED_MSB: [0x18, 0x19, 0x1A, 0x1B],   // Playing speed
    SPEED_LSB: [0x38, 0x39, 0x3A, 0x3B]
};

// Single comprehensive pad mode mapping system
DDJFLX10.PAD_MODES = {
    // Mode definitions
    HOTCUE: 'hotcue',
    BEATLOOP: 'beatloop',
    BEATJUMP: 'beatjump',
    SAMPLER: 'sampler',
    PADFX: 'padfx',
    
    // Mode to index mapping (for LED calculations)
    //  Can be changed
    TO_INDEX: {
        'hotcue': 0,
        'beatloop': 1,
        'beatjump': 2,
        'sampler': 3,
        'padfx': 4
    },
    
    // Index to mode/page mapping (for pad decoding)
    FROM_INDEX: [
        { mode: 'hotcue', page: 1 },  // 0: HotCue Page 1
        { mode: 'hotcue', page: 2 },  // 1: HotCue Page 2
        { mode: 'padfx', page: 1 },   // 2: PadFX Page 1
        { mode: 'padfx', page: 2 }    // 3: PadFX Page 2
    ]
};

// =============================================================================
// ===== GLOBAL STATE VARIABLES =====
// =============================================================================
// Component Containers
DDJFLX10.channelContainers = [];
DDJFLX10.padContainers = [];

// Pad modes per deck
DDJFLX10.padModes = {};

// Jog display state
DDJFLX10.timeModeState = [0x00, 0x00, 0x00, 0x00];

// Per-deck state
DDJFLX10.shiftButtonDown = [false, false, false, false];
DDJFLX10.loopAdjustIn = [false, false, false, false];
DDJFLX10.loopAdjustOut = [false, false, false, false];
DDJFLX10.activeLeftDeck = 1;
DDJFLX10.activeRightDeck = 2;

// Timers
DDJFLX10.timers = {};
DDJFLX10.bendResetTimers = {};
DDJFLX10.jogRingFlashTimers = {};

// VU meters
DDJFLX10.vuMeters = {};

// =============================================================================
// ===== HELPER FUNCTIONS =====
// =============================================================================
// Centralized debug handler
DDJFLX10.debug = function(category, message) {
    switch(category) {
        case 'pad':
            if (DDJFLX10.USER_CONFIG.debugPadInput) {
                console.log('[PAD] ' + message);
            }
            break;
        case 'jog':
            if (DDJFLX10.USER_CONFIG.debugJogDisplay) {
                console.log('[JOG] ' + message);
            }
            break;
        case 'midi':
            if (DDJFLX10.USER_CONFIG.debugMidi) {
                console.log('[MIDI] ' + message);
            }
            break;
        case 'jogmidi':
            if (DDJFLX10.USER_CONFIG.debugMidi || DDJFLX10.USER_CONFIG.debugJogDisplay) {
                console.log('[JOG-MIDI] ' + message);
            }
            break;
    }
};

// Extract deck number from group string
DDJFLX10.deckFromGroup = function(group) {
    let match = group.match(/\[Channel(\d+)\]/);
    return match ? parseInt(match[1]) : null;
};

// Decode incoming pad MIDI message
DDJFLX10.decodePadMidi = function(status, data1, data2) {
    // Status encodes deck: 0x97=Deck1, 0x99=Deck2, 0x9B=Deck3, 0x9D=Deck4
    const deck = Math.floor((status - 0x97) / 2) + 1;
    const shifted = ((status - 0x97) % 2) === 1;
    
    // Data1 encodes pad (1-8) and mode-page index
    const pad = (data1 % 8) + 1;  // 1-8
    const modePageIndex = Math.floor(data1 / 8);
    
    // Use the centralized pad mode mapping
    const modeInfo = DDJFLX10.PAD_MODES.FROM_INDEX[modePageIndex] || { mode: 'unknown', page: 1 };
    
    return {
        deck: deck,
        pad: pad,
        mode: modeInfo.mode,
        page: modeInfo.page,
        modePageIndex: modePageIndex,
        shifted: shifted,
        pressed: data2 === 0x7F
    };
};

// Encode pad info to MIDI message for LED output
DDJFLX10.encodePadMidi = function(deck, pad, modePageIndex, on) {
    const status = 0x97 + (deck - 1) * 2;
    const data1 = (modePageIndex * 8) + (pad - 1);
    const data2 = on ? 0x7F : 0x00;
    return { status, data1, data2 };
};

// Helper clamp function
DDJFLX10._clamp = function(value, min, max) {
    return Math.max(min, Math.min(max, value));
};

// Helper function to send MSB/LSB CC messages
DDJFLX10._sendMsbLsbCC = function(deckNum, msbControls, lsbControls, value14bit) {
    const index = deckNum - 1;
    if (index < 0 || index >= DDJFLX10.DECKS.COUNT) {
        return;
    }
    const msb = (value14bit >> 7) & 0x7F;
    const lsb = value14bit & 0x7F;
    
    if (DDJFLX10.USER_CONFIG.debugMidi || DDJFLX10.USER_CONFIG.debugJogDisplay) {
        DDJFLX10.debug('jogmidi', 'Sending jog CC: BF ' + msbControls[index].toString(16).toUpperCase() + ' ' + msb.toString(16) + 
                    ', BF ' + lsbControls[index].toString(16).toUpperCase() + ' ' + lsb.toString(16) + 
                    ' (deck=' + deckNum + ', value=' + value14bit + ')');
    }
    DDJFLX10._outShort(DDJFLX10.MIDI.JOG_DISPLAY_CC, msbControls[index], msb);
    DDJFLX10._outShort(DDJFLX10.MIDI.JOG_DISPLAY_CC, lsbControls[index], lsb);
};

// Send degrees as CC
DDJFLX10._sendDegreesCC = function(deckNum, msbControls, lsbControls, degrees) {
    const deg = DDJFLX10._clamp(Math.round(degrees), 0, 359);
    DDJFLX10._sendMsbLsbCC(deckNum, msbControls, lsbControls, deg);
};

// Pulse control helper
DDJFLX10.pulseControl = function(group, control) {
    engine.setValue(group, control, 1);
    engine.beginTimer(20, function() { engine.setValue(group, control, 0); }, true);
};

// =============================================================================
// ===== INPUT HANDLERS =====
// =============================================================================
// Pad input handler (using confirmed DDJ-FLX10 MIDI addressing)
DDJFLX10.padInputHandler = function(channel, control, value, status) {
    // Debug logging
    if (DDJFLX10.USER_CONFIG.debugPadInput) {
        DDJFLX10.debug('pad', `Status: 0x${status.toString(16).toUpperCase()}, ` +
                    `Data1: 0x${control.toString(16).toUpperCase()} (${control}), ` +
                    `Data2: 0x${value.toString(16).toUpperCase()}`);
    }
    
    // Decode the MIDI message
    const padInfo = DDJFLX10.decodePadMidi(status, control, value);
    const group = `[Channel${padInfo.deck}]`;
    
    // Handle based on mode
    switch (padInfo.mode) {
        case 'hotcue':
            // Only handle press events for hotcue
            if (!padInfo.pressed) return;
            
            // Calculate hotcue number: (page-1)*8 + pad = 1-16
            const hotcueNum = ((padInfo.page - 1) * 8) + padInfo.pad;
            
            if (padInfo.shifted) {
                DDJFLX10.pulseControl(group, `hotcue_${hotcueNum}_clear`);
            } else {
                DDJFLX10.pulseControl(group, `hotcue_${hotcueNum}_activate`);
            }
            break;
            
        case 'padfx':
            // PadFX: Beat loop rolls (slip loops) - need press AND release
            const sizes = DDJFLX10.PADFX_SIZES[padInfo.page] || DDJFLX10.PADFX_SIZES[1];
            const loopSize = sizes[padInfo.pad - 1];
            
            if (padInfo.pressed) {
                // Activate beat loop roll on press
                engine.setValue(group, `beatlooproll_${loopSize}_activate`, 1);
            } else {
                // Deactivate on release - slip back to original position
                engine.setValue(group, `beatlooproll_${loopSize}_activate`, 0);
            }
            break;
            
        case 'beatjump':
            // Only handle press events for beat jump
            if (!padInfo.pressed) return;
            
            const jumpSizes = DDJFLX10.BEATJUMP_SIZES[padInfo.page] || DDJFLX10.BEATJUMP_SIZES[1];
            const jumpSize = jumpSizes[padInfo.pad - 1];
            
            // Pads 1-4: jump backward, Pads 5-8: jump forward
            if (padInfo.pad <= 4) {
                engine.setValue(group, 'beatjump_size', jumpSize);
                DDJFLX10.pulseControl(group, 'beatjump_backward');
            } else {
                engine.setValue(group, 'beatjump_size', jumpSizes[padInfo.pad - 5]);
                DDJFLX10.pulseControl(group, 'beatjump_forward');
            }
            break;
            
        default:
            console.warn(`[PAD] Unknown mode: ${padInfo.mode}`);
    }
};

// Cycle through tempo ranges (Shift + Tempo Reset)
DDJFLX10.cycleTempoRange = function(channel, control, value, status, group) {
    if (value === 0) return; // ignore release
    
    var currRange = engine.getValue(group, "rateRange");
    var idx = 0;
    
    for (var i = 0; i < DDJFLX10.TEMPO_RANGES.length; i++) {
        if (currRange <= DDJFLX10.TEMPO_RANGES[i]) {
            idx = (i + 1) % DDJFLX10.TEMPO_RANGES.length;
            break;
        }
    }
    engine.setValue(group, "rateRange", DDJFLX10.TEMPO_RANGES[idx]);
};

// Jog touch handler - enables/disables scratching
DDJFLX10.jogTouchHandler = function(channel, control, value, status, group) {
    var deckNumber = script.deckFromGroup(group);
    
    if ((status & 0xF0) === 0x90 && value > 0) {  // Note ON with velocity
        // Touch ON - enable scratching
        engine.scratchEnable(deckNumber, 
            DDJFLX10.JOG_CONFIG.RESOLUTION,
            DDJFLX10.JOG_CONFIG.RPM,
            DDJFLX10.JOG_CONFIG.ALPHA,
            DDJFLX10.JOG_CONFIG.BETA,
            true  // ramp: true for smooth speed transition
        );
    } else {
        // Touch OFF - disable scratching
        engine.scratchDisable(deckNumber, true);
    }
};

// Jog wheel rotation handler
DDJFLX10.jogInputHandler = function(channel, control, value, status, group) {
    var deckNumber = script.deckFromGroup(group);
    
    // Convert to signed: CW = positive, CCW = negative
    var newValue = value - 64;

    // Beat Jump jog mode (CC 0x29)
    if (control === 0x29) {
        if (newValue === 0) {
            return;
        }
        engine.setValue(group, 'beatjump_size', DDJFLX10.USER_CONFIG.quickJumpSize);
        if (newValue > 0) {
            DDJFLX10.pulseControl(group, 'beatjump_forward');
        } else {
            DDJFLX10.pulseControl(group, 'beatjump_backward');
        }
        return;
    }
    
    // Register the movement
    if (engine.isScratching(deckNumber)) {
        engine.scratchTick(deckNumber, newValue);  // Scratch!
    } else {
        // Pitch bend
        engine.setValue(group, 'wheel', newValue * DDJFLX10.JOG_CONFIG.BEND_SCALE);

        var timerKey = String(deckNumber);
        if (DDJFLX10.bendResetTimers[timerKey]) {
            engine.stopTimer(DDJFLX10.bendResetTimers[timerKey]);
            DDJFLX10.bendResetTimers[timerKey] = null;
        }
        DDJFLX10.bendResetTimers[timerKey] = engine.beginTimer(
            DDJFLX10.JOG_CONFIG.BEND_RESET_MS,
            function() {
                engine.setValue(group, 'wheel', 0);
                DDJFLX10.bendResetTimers[timerKey] = null;
            },
            true
        );
    }
};

// Beat jump handlers
DDJFLX10.beatjump = function(direction, channel, control, value, status, group) {
    if (!value) {
        return;
    }
    var size = 4;
    if (control === 0x61 || control === 0x62) {
        size = 16;
    } else if (control === 0x70 || control === 0x71) {
        size = 32;
    }
    engine.setValue(group, 'beatjump_size', size);
    DDJFLX10.pulseControl(group, direction === 'forward' ? 'beatjump_forward' : 'beatjump_backward');
};

// Time mode handler (switches between elapsed/remaining)
DDJFLX10.timeModeHandler = function(channel, control, value, status, group) {
    // Control values: 0x00 = elapsed, 0x7F = remaining
    // Data1: 0x14=Deck1, 0x15=Deck2, 0x16=Deck3, 0x17=Deck4
    const deckNum = control - 0x14 + 1; // Convert 0x14-0x17 to 1-4
    
    if (deckNum >= 1 && deckNum <= DDJFLX10.DECKS.COUNT) {
        DDJFLX10.timeModeState[deckNum - 1] = value;
        // Force update time display with new mode
        const deckGroup = '[Channel' + deckNum + ']';
        DDJFLX10.updateJogTime(null, deckGroup, 'playposition');
    }
};

DDJFLX10.beatjumpBackward = function(channel, control, value, status, group) {
    DDJFLX10.beatjump('backward', channel, control, value, status, group);
};

DDJFLX10.beatjumpForward = function(channel, control, value, status, group) {
    DDJFLX10.beatjump('forward', channel, control, value, status, group);
};

// Waveform zoom (applies to all decks)
DDJFLX10.waveformZoom = function(channel, control, value, status, group) {
    var zoomControl = (value === 0x7F) ? "waveform_zoom_up" : "waveform_zoom_down";
    for (var i = 1; i <= DDJFLX10.DECKS.COUNT; i++) {
        engine.setValue("[Channel" + i + "]", zoomControl, 0.125); // 12.5% zoom per step
    }                                                              // needs testing
};

// =============================================================================
// ===== OUTPUT HANDLERS =====
// =============================================================================
// Pad LED update handler
DDJFLX10.updatePadLed = function(value, group, control) {
    let deck = DDJFLX10.deckFromGroup(group);
    if (!deck || deck > DDJFLX10.DECKS.COUNT) return;

    let numMatch = control.match(/_(\d+)_/);
    if (!numMatch) return;
    let num = parseInt(numMatch[1]);
    
    let page = Math.floor((num - 1) / DDJFLX10.PAD_CONFIG.PER_PAGE);
    let localPad = ((num - 1) % DDJFLX10.PAD_CONFIG.PER_PAGE) + 1;

    let colorKey = control.replace('status', 'color');
    let rgb = engine.getValue(group, colorKey) || 0xFFFFFF;
    
    let colorValue = value ? (DDJFLX10.mapColor ? DDJFLX10.mapColor(rgb) : DDJFLX10.MIDI.LED_ON) : DDJFLX10.MIDI.LED_OFF;

    const msg = DDJFLX10.encodePadMidi(deck, localPad, page, colorValue > 0);
    DDJFLX10._outShort(msg.status, msg.data1, colorValue);
};

// Update all pad LEDs for a group
DDJFLX10.updateAllPadLeds = function(group) {
    let mode = DDJFLX10.padModes[group];
    
    for (let p = 0; p < 2; p++) {
        for (let pad = 1; pad <= DDJFLX10.PAD_CONFIG.PER_PAGE; pad++) {
            let num = (p * DDJFLX10.PAD_CONFIG.PER_PAGE) + pad;
            if (mode === 'hotcue') {
                DDJFLX10.updatePadLed(
                    engine.getValue(group, 'hotcue_' + num + '_status'), 
                    group, 
                    'hotcue_' + num + '_status'
                );
            }
        }
    }
};

// Set pad mode
DDJFLX10.setPadMode = function(channel, control, value, status, group, mode) {
    if (value) {
        DDJFLX10.padModes[group] = mode;
        DDJFLX10.updateAllPadLeds(group);
    }
};

// Send pad LED message
DDJFLX10.sendPadLed = function(deck, pad, modePageIndex, colorValue) {
    const msg = DDJFLX10.encodePadMidi(deck, pad, modePageIndex, colorValue > 0);
    DDJFLX10._outShort(msg.status, msg.data1, colorValue);
};

// Jog marker (digital marker): playposition -> degrees
DDJFLX10.track_marker = function(value, group, control) {
    const deckNum = DDJFLX10.deckFromGroup(group);
    const position = engine.getValue(group, 'playposition');
    if (DDJFLX10.USER_CONFIG.debugJogDisplay) {
        DDJFLX10.debug('jog', 'track_marker: deck=' + deckNum + ' position=' + position + ' group=' + group);
    }
    if (deckNum === null || isNaN(position)) {
        return;
    }
    const degrees = position * 359;
    DDJFLX10._sendDegreesCC(deckNum, DDJFLX10.JOG_DISPLAY.MARKER_MSB, DDJFLX10.JOG_DISPLAY.MARKER_LSB, degrees);
};

// Update jog BPM display (0.0 to 999.9 BPM)
DDJFLX10.track_bpm = function (value, group, control) {
    var deckNum = DDJFLX10.deckFromGroup(group);
    var index = deckNum - 1;
    var bpm = engine.getValue(group, 'bpm');
    if (isNaN(bpm)) return;
    var bpm10 = Math.round(bpm * 10); // BPM * 10 for precision (0-9999)
    var msb = Math.floor(bpm10 / 128);
    var lsb = bpm10 % 128;
    if (DDJFLX10.USER_CONFIG.debugJogDisplay) {
        DDJFLX10.debug('jog', 'BPM: deck=' + deckNum + ' bpm=' + bpm + ' msb=' + msb + ' lsb=' + lsb);
    }
    DDJFLX10._outShort(DDJFLX10.MIDI.JOG_DISPLAY_CC, DDJFLX10.JOG_DISPLAY.BPM_MSB[index], msb);
    DDJFLX10._outShort(DDJFLX10.MIDI.JOG_DISPLAY_CC, DDJFLX10.JOG_DISPLAY.BPM_LSB[index], lsb);
};

// Update jog playing speed display (-100.0% to +100.0%)
DDJFLX10.track_playing_speed = function (value, group, control) {
    var deckNum = DDJFLX10.deckFromGroup(group);
    var index = deckNum - 1;
    var rate = engine.getValue(group, 'rate');
    if (isNaN(rate)) return;
    var percent = rate * 100;
    var percent10 = Math.round((percent + 100) * 10);
    var msb = Math.floor(percent10 / 128);
    var lsb = percent10 % 128;
    if (DDJFLX10.USER_CONFIG.debugJogDisplay) {
        DDJFLX10.debug('jog', 'Playing speed: deck=' + deckNum + ' rate=' + rate + ' percent=' + percent + ' msb=' + msb + ' lsb=' + lsb);
    }
    DDJFLX10._outShort(DDJFLX10.MIDI.JOG_DISPLAY_CC, DDJFLX10.JOG_DISPLAY.SPEED_MSB[index], msb);
    DDJFLX10._outShort(DDJFLX10.MIDI.JOG_DISPLAY_CC, DDJFLX10.JOG_DISPLAY.SPEED_LSB[index], lsb);
};

// Update jog time display
DDJFLX10.updateJogTime = function (value, group, control) {
    var deckNum = DDJFLX10.deckFromGroup(group);
    var index = deckNum - 1;
    var duration = engine.getValue(group, 'duration');
    var position = engine.getValue(group, 'playposition');
    
    if (isNaN(duration) || isNaN(position)) {
        return;
    }
    
    var time;
    if (DDJFLX10.timeModeState[index] === 0x7F) {
        time = (1 - position) * duration;  // Remaining time
    } else {
        time = position * duration;       // Elapsed time
    }
    
    var min = Math.floor(time / 60);
    var sec = Math.floor(time % 60);
    
    // Send time to jog display (Channel 16 CC)
    DDJFLX10._outShort(DDJFLX10.MIDI.JOG_DISPLAY_CC, DDJFLX10.JOG_DISPLAY.TIME_MIN[index], min);
    DDJFLX10._outShort(DDJFLX10.MIDI.JOG_DISPLAY_CC, DDJFLX10.JOG_DISPLAY.TIME_SEC[index], sec);
};

// Update jog duration display
DDJFLX10.updateJogDuration = function(value, group, control) {
    var deckNum = DDJFLX10.deckFromGroup(group);
    var index = deckNum - 1;
    var duration = engine.getValue(group, 'duration');
    
    if (isNaN(duration)) {
        return;
    }
    
    // Duration is always shown as total time
    var min = Math.floor(duration / 60);
    var sec = Math.floor(duration % 60);
};

DDJFLX10._sendJogNote = function(deckNum, data1List, on) {
    var index = deckNum - 1;
    if (index < 0 || index >= DDJFLX10.DECKS.COUNT) {
        return;
    }
    DDJFLX10._outShort(
        DDJFLX10.MIDI.JOG_DISPLAY_NOTE,
        data1List[index],
        on ? DDJFLX10.MIDI.LED_ON : DDJFLX10.MIDI.LED_OFF
    );
};

// Sheet 6: MASTER (Beat Sync) — Mixxx sync_leader
DDJFLX10.track_sync_leader = function(value, group, control) {
    var deckNum = DDJFLX10.deckFromGroup(group);
    if (deckNum === null) {
        return;
    }
    DDJFLX10._sendJogNote(deckNum, DDJFLX10.JOG_DISPLAY.MASTER, !!value);
};

// Sheet 6: SYNC (Beat Sync) — Mixxx sync_enabled
DDJFLX10.track_sync_enabled = function(value, group, control) {
    var deckNum = DDJFLX10.deckFromGroup(group);
    if (deckNum === null) {
        return;
    }
    DDJFLX10._sendJogNote(deckNum, DDJFLX10.JOG_DISPLAY.SYNC, !!value);
};

// Sheet 6: Master tempo — Mixxx keylock
DDJFLX10.track_keylock = function(value, group, control) {
    var deckNum = DDJFLX10.deckFromGroup(group);
    if (deckNum === null) {
        return;
    }
    DDJFLX10._sendJogNote(deckNum, DDJFLX10.JOG_DISPLAY.KEYLOCK, !!value);
};

// Sheet 6: Key — deck CC Bn / 0x49, Data2 0x00-0x18
DDJFLX10.track_key = function(value, group, control) {
    var deckNum = DDJFLX10.deckFromGroup(group);
    if (deckNum === null) {
        return;
    }
    var mixxxKey = Math.round(engine.getValue(group, 'key'));
    var pioneerKey = DDJFLX10.PIONEER_KEY_FROM_MIXXX[mixxxKey] || 0x00;
    DDJFLX10._outShort(DDJFLX10.MIDI.CC + (deckNum - 1), DDJFLX10.JOG_DISPLAY.KEY, pioneerKey);
};

// Sheet 6: Cue point needle, or hide 7F/7F
DDJFLX10.track_cue_point = function(value, group, control) {
    var deckNum = DDJFLX10.deckFromGroup(group);
    if (deckNum === null) {
        return;
    }
    var cue = engine.getValue(group, 'cue_point');
    var samples = engine.getValue(group, 'track_samples');
    if (cue < 0 || !samples) {
        DDJFLX10._outShort(DDJFLX10.MIDI.JOG_DISPLAY_CC, DDJFLX10.JOG_DISPLAY.CUE_MSB[deckNum - 1], 0x7F);
        DDJFLX10._outShort(DDJFLX10.MIDI.JOG_DISPLAY_CC, DDJFLX10.JOG_DISPLAY.CUE_LSB[deckNum - 1], 0x7F);
        return;
    }
    DDJFLX10._sendDegreesCC(
        deckNum,
        DDJFLX10.JOG_DISPLAY.CUE_MSB,
        DDJFLX10.JOG_DISPLAY.CUE_LSB,
        (cue / samples) * 359
    );
};

// =============================================================================
// ===== INITIALIZATION FUNCTIONS =====
// =============================================================================
// Initialize state
DDJFLX10.initState = function() {
    DDJFLX10.padModes = {};
    DDJFLX10.vuMeters = {};
    DDJFLX10.shiftButtonDown = new Array(DDJFLX10.DECKS.COUNT).fill(false);
    DDJFLX10.loopAdjustIn = new Array(DDJFLX10.DECKS.COUNT).fill(false);
    DDJFLX10.loopAdjustOut = new Array(DDJFLX10.DECKS.COUNT).fill(false);
    
    // Initialize pad modes for each channel
    for (let i = 1; i <= DDJFLX10.DECKS.COUNT; i++) {
        const group = `[Channel${i}]`;
        DDJFLX10.padModes[group] = DDJFLX10.PAD_MODES.HOTCUE;
    }
};

// Initialize pad LED outputs and connections
DDJFLX10._initPadOutputs = function() {
    for (let ch = 0; ch < DDJFLX10.DECKS.COUNT; ch++) {
        let group = '[Channel' + (ch + 1) + ']';
        for (let num = 1; num <= DDJFLX10.PAD_CONFIG.COUNT; num++) {
            engine.makeConnection(group, 'hotcue_' + num + '_status', DDJFLX10.updatePadLed);
            engine.makeConnection(group, 'hotcue_' + num + '_color', DDJFLX10.updatePadLed);
        }
    }
};

// Initialize jog display outputs and connections
DDJFLX10._initJogDisplayOutputs = function() {
    if (!DDJFLX10.USER_CONFIG.enableJogDisplay) {
        return;
    }
    
    for (let ch = 0; ch < DDJFLX10.DECKS.COUNT; ch++) {
        let group = '[Channel' + (ch + 1) + ']';
        const deckNum = ch + 1;

        // Ensure jog info is visible (Sheet 6: 9F 5D-60, 0x00 = display)
        DDJFLX10._outShort(DDJFLX10.MIDI.JOG_DISPLAY_NOTE, DDJFLX10.JOG_DISPLAY.VISIBILITY[ch], 0x00);

        engine.makeConnection(group, 'playposition', DDJFLX10.track_marker);
        engine.makeConnection(group, 'bpm', DDJFLX10.track_bpm);
        engine.makeConnection(group, 'rate', DDJFLX10.track_playing_speed);
        engine.makeConnection(group, 'playposition', DDJFLX10.updateJogTime);
        engine.makeConnection(group, 'duration', DDJFLX10.updateJogDuration);
        engine.makeConnection(group, 'sync_leader', DDJFLX10.track_sync_leader);
        engine.makeConnection(group, 'sync_enabled', DDJFLX10.track_sync_enabled);
        engine.makeConnection(group, 'keylock', DDJFLX10.track_keylock);
        engine.makeConnection(group, 'key', DDJFLX10.track_key);
        engine.makeConnection(group, 'cue_point', DDJFLX10.track_cue_point);

        DDJFLX10.track_marker(null, group, 'playposition');
        DDJFLX10.track_bpm(null, group, 'bpm');
        DDJFLX10.track_playing_speed(null, group, 'rate');
        DDJFLX10.updateJogTime(null, group, 'playposition');
        DDJFLX10.updateJogDuration(null, group, 'duration');
        DDJFLX10.track_sync_leader(engine.getValue(group, 'sync_leader'), group, 'sync_leader');
        DDJFLX10.track_sync_enabled(engine.getValue(group, 'sync_enabled'), group, 'sync_enabled');
        DDJFLX10.track_keylock(engine.getValue(group, 'keylock'), group, 'keylock');
        DDJFLX10.track_key(engine.getValue(group, 'key'), group, 'key');
        DDJFLX10.track_cue_point(engine.getValue(group, 'cue_point'), group, 'cue_point');
    }
};

// Initialize VU meter outputs and connections
DDJFLX10._initVuMeterOutputs = function() {
    if (!DDJFLX10.USER_CONFIG.enableVuMeters) {
        return;
    }
    
    for (let ch = 1; ch <= DDJFLX10.DECKS.COUNT; ch++) {
        let vuOptions = { 
            group: '[Channel' + ch + ']',
            outKey: 'vu_meter',
            output: function(value) {
                let level = Math.round(value * 127);  
                let status = DDJFLX10.MIDI.CC + (this.channelIndex || ch - 1);  
                DDJFLX10._outShort(status, 0x02, level); 
            }
        };
        
        // Fix closure issue by storing channel index
        vuOptions.channelIndex = ch - 1;
        
        // Direct engine connection (no Components JS)
        const channelIndex = vuOptions.channelIndex;
        DDJFLX10.vuMeters[ch] = engine.makeConnection(
            vuOptions.group,
            vuOptions.outKey,
            function(value) {
                DDJFLX10._outShort(DDJFLX10.MIDI.CC + channelIndex, 0x02, Math.round(value * 127));
            }
        );
    }
};

// HID daemon IPC — emits deck state as private SysEx (F0 7D ...) to the relay
// daemon. No log tailing: the daemon reads it off the MIDI wire. (see _ipc* below)
DDJFLX10._initHidDaemonIpc = function() {
    if (!DDJFLX10.USER_CONFIG.enableHidDaemonIpc) {
        return;
    }
    DDJFLX10._lastLoggedDuration = {1: 0, 2: 0, 3: 0, 4: 0};
    DDJFLX10._lastLoggedPos = {1: -1, 2: -1, 3: -1, 4: -1};
    DDJFLX10._lastLoggedBpm = {1: 0, 2: 0, 3: 0, 4: 0};

    DDJFLX10._logTrackLoadDeferred = function(deck) {
        var group = '[Channel' + deck + ']';
        var dur = engine.getValue(group, 'duration');
        var samples = engine.getValue(group, 'track_samples');
        var fbpm = engine.getValue(group, 'file_bpm');
        if (dur > 0 && samples > 0) {
            DDJFLX10._ipcTrackLoad(deck, samples, fbpm, dur);
            var rateRatio = engine.getValue(group, 'rate_ratio');
            var liveBpm = fbpm * rateRatio;
            var rounded = Math.round(liveBpm * 10) / 10;
            DDJFLX10._lastLoggedBpm[deck] = rounded;
            DDJFLX10._ipcBpm(deck, rounded);
        }
    };

    for (var d = 1; d <= DDJFLX10.DECKS.COUNT; d++) {
        (function(deck) {
            engine.makeConnection('[Channel' + deck + ']', 'duration', function(value) {
                if (value > 0 && value !== DDJFLX10._lastLoggedDuration[deck]) {
                    DDJFLX10._lastLoggedDuration[deck] = value;
                    engine.beginTimer(300, function() {
                        DDJFLX10._logTrackLoadDeferred(deck);
                    }, true);
                }
            });
        })(d);
    }

    DDJFLX10.hidIpcTimer = engine.beginTimer(100, function() {
        for (var dd = 1; dd <= DDJFLX10.DECKS.COUNT; dd++) {
            var grp = '[Channel' + dd + ']';
            var dur = engine.getValue(grp, 'duration');
            if (dur <= 0) {
                continue;
            }
            var pos = engine.getValue(grp, 'playposition');
            var posRounded = Math.round(pos * 10000) / 10000;
            if (posRounded !== DDJFLX10._lastLoggedPos[dd]) {
                DDJFLX10._lastLoggedPos[dd] = posRounded;
                DDJFLX10._ipcPos(dd, pos);
            }
            var fbpm = engine.getValue(grp, 'file_bpm');
            var rateRatio = engine.getValue(grp, 'rate_ratio');
            var liveBpm = fbpm * rateRatio;
            var rounded = Math.round(liveBpm * 10) / 10;
            if (rounded !== DDJFLX10._lastLoggedBpm[dd]) {
                DDJFLX10._lastLoggedBpm[dd] = rounded;
                DDJFLX10._ipcBpm(dd, rounded);
            }
        }
    }, false);
};

// Rekordbox-mode SysEx (Veezuhz capture). Puts jog firmware in waveform mode.
// Serato handshake does not.
DDJFLX10._SYSEX_RKBOX_KEEPALIVE = [
    0xF0, 0x00, 0x40, 0x05, 0x00, 0x00, 0x04, 0x01, 0x00, 0x50, 0x00, 0xF7
];
DDJFLX10._SYSEX_ENTER_HID = [
    0xF0, 0x00, 0x40, 0x05, 0x00, 0x00, 0x04, 0x01, 0x00, 0x03, 0x01, 0xF7
];
DDJFLX10._SYSEX_RKBOX_DECK_INIT = {
    1: [0xF0, 0x00, 0x40, 0x05, 0x00, 0x00, 0x04, 0x01,
        0x00, 0x11, 0x0F, 0x0F, 0x0F, 0x0F, 0x0F, 0x0F, 0xF7],
    2: [0xF0, 0x00, 0x40, 0x05, 0x00, 0x00, 0x04, 0x01,
        0x00, 0x12, 0x0F, 0x0F, 0x0F, 0x0F, 0x0F, 0x0F, 0xF7],
    3: [0xF0, 0x00, 0x40, 0x05, 0x00, 0x00, 0x04, 0x01,
        0x00, 0x13, 0x0F, 0x0F, 0x0F, 0x0F, 0x0F, 0x0F, 0xF7],
    4: [0xF0, 0x00, 0x40, 0x05, 0x00, 0x00, 0x04, 0x01,
        0x00, 0x14, 0x0F, 0x0F, 0x0F, 0x0F, 0x0F, 0x0F, 0xF7]
};
DDJFLX10._SYSEX_GLOBAL_B = [
    0xF0, 0x00, 0x40, 0x05, 0x00, 0x00, 0x04, 0x01,
    0x00, 0x0B, 0x31, 0x00, 0x00, 0x00, 0x00, 0x00, 0xF7
];
DDJFLX10._SYSEX_RKBOX_GLOBAL_C = [
    0xF0, 0x00, 0x40, 0x05, 0x00, 0x00, 0x04, 0x01,
    0x00, 0x00, 0x0C, 0x00, 0x00, 0x02, 0x0E, 0x0E, 0x00, 0x00, 0x00, 0xF7
];
DDJFLX10._SYSEX_RKBOX_MODE_CONFIG = [
    0xF0, 0x00, 0x40, 0x05, 0x00, 0x00, 0x04, 0x01,
    0x00, 0x00, 0x0A, 0x00, 0x28, 0x00, 0x26, 0x00,
    0x18, 0x3D, 0x3A, 0x05, 0x64, 0x50, 0x2A, 0x54,
    0x40, 0x14, 0x1A, 0x04, 0x69, 0x13,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0xF7
];

DDJFLX10._sendSysex = function(bytes) {
    midi.sendSysexMsg(bytes, bytes.length);
};

// ---- Private IPC SysEx -> flx10_relay_daemon (replaces log-tailing) --------
// The daemon owns the FLX10 via libusb and sits in the MIDI relay path, so it
// reads these off the wire. 0x7D = the reserved "private/educational" SysEx ID,
// so it never collides with real Pioneer commands or the mapping spreadsheet.
// The daemon CONSUMES F0 7D ... (does not forward it to the controller).
// Values are 7-bit packed (MIDI data bytes are 0..127), big-endian groups.
//   type 0x01 track load : deck, samples(4x7), file_bpm*100(3x7), dur_ms(3x7)
//   type 0x02 position   : deck, pos*1e6(3x7)
//   type 0x03 bpm        : deck, live_bpm*100(3x7)
DDJFLX10._IPC_ID = 0x7D;
DDJFLX10._pack7 = function(value, nbytes) {
    var out = [];
    value = Math.round(value);
    if (value < 0) { value = 0; }
    for (var i = nbytes - 1; i >= 0; i--) {
        out.push((Math.floor(value / Math.pow(128, i))) & 0x7F);
    }
    return out;
};
DDJFLX10._ipcTrackLoad = function(deck, samples, fileBpm, durationSec) {
    var msg = [0xF0, DDJFLX10._IPC_ID, 0x01, deck & 0x7F]
        .concat(DDJFLX10._pack7(samples, 4))
        .concat(DDJFLX10._pack7(fileBpm * 100, 3))
        .concat(DDJFLX10._pack7(durationSec * 1000, 3));
    msg.push(0xF7);
    DDJFLX10._sendSysex(msg);
};
DDJFLX10._ipcPos = function(deck, pos) {
    var msg = [0xF0, DDJFLX10._IPC_ID, 0x02, deck & 0x7F]
        .concat(DDJFLX10._pack7(pos * 1000000, 3));
    msg.push(0xF7);
    DDJFLX10._sendSysex(msg);
};
DDJFLX10._ipcBpm = function(deck, bpm) {
    var msg = [0xF0, DDJFLX10._IPC_ID, 0x03, deck & 0x7F]
        .concat(DDJFLX10._pack7(bpm * 100, 3));
    msg.push(0xF7);
    DDJFLX10._sendSysex(msg);
};

DDJFLX10._initRekordboxSysex = function() {
    if (!DDJFLX10.USER_CONFIG.enableRekordboxSysex) {
        return;
    }
    try {
        DDJFLX10._sendSysex(DDJFLX10._SYSEX_RKBOX_KEEPALIVE);
        for (var d = 1; d <= 4; d++) {
            DDJFLX10._sendSysex(DDJFLX10._SYSEX_RKBOX_DECK_INIT[d]);
        }
        console.log('FLX10: rekordbox EARLY SysEx sent; LATE in 5s');
        DDJFLX10.sysexKeepaliveTimer = engine.beginTimer(200, function() {
            DDJFLX10._sendSysex(DDJFLX10._SYSEX_RKBOX_KEEPALIVE);
        }, false);
        engine.beginTimer(5000, function() {
            DDJFLX10._sendSysex(DDJFLX10._SYSEX_GLOBAL_B);
            DDJFLX10._sendSysex(DDJFLX10._SYSEX_RKBOX_GLOBAL_C);
            DDJFLX10._sendSysex(DDJFLX10._SYSEX_RKBOX_MODE_CONFIG);
            DDJFLX10._sendSysex(DDJFLX10._SYSEX_ENTER_HID);
            console.log('FLX10: rekordbox LATE SysEx sent (mode-enable)');
        }, true);
    } catch (e) {
        console.log('FLX10: rekordbox SysEx failed: ' + e);
    }
};

// =============================================================================
// ===== MAIN INITIALIZATION & SHUTDOWN =====
// =============================================================================
DDJFLX10.init = function(id, debugging) {
    // Initialize state
    DDJFLX10.initState();
    
    // Initialize Component Containers
    DDJFLX10.channelContainers = [];
    DDJFLX10.padContainers = [];  
    for (let ch = 1; ch <= DDJFLX10.DECKS.COUNT; ch++) {
        let group = '[Channel' + ch + ']';
        DDJFLX10.channelContainers[ch] = {group: group};
        DDJFLX10.padContainers[ch] = {group: group};
        DDJFLX10.padModes[group] = DDJFLX10.PAD_MODES.HOTCUE;
    }
    
    // Register Pad MIDI Input Handlers
    for (let i = 0; i < DDJFLX10.DECKS.COUNT; i++) {
        const status = DDJFLX10.MIDI.PAD_STATUS[i * 2];
        const shiftedStatus = DDJFLX10.MIDI.PAD_STATUS[i * 2 + 1];
        for (let midino = 0; midino < 32; midino++) {
            midi.makeInputHandler(status, midino, DDJFLX10.padInputHandler);
            midi.makeInputHandler(shiftedStatus, midino, DDJFLX10.padInputHandler);
        }
    }
    
    // Register Time Mode Input Handlers (Channel 16 Note)
    // Data1: 0x14=Deck1, 0x15=Deck2, 0x16=Deck3, 0x17=Deck4
    for (let i = 0; i < DDJFLX10.DECKS.COUNT; i++) {
        const control = 0x14 + i; // 0x14, 0x15, 0x16, 0x17
        midi.makeInputHandler(DDJFLX10.MIDI.JOG_DISPLAY_NOTE, control, DDJFLX10.timeModeHandler);
    }
    
    // Initialize All Outputs
    DDJFLX10._initPadOutputs();
    DDJFLX10._initJogDisplayOutputs();
    DDJFLX10._initVuMeterOutputs();
    DDJFLX10._initHidDaemonIpc();
    DDJFLX10._initRekordboxSysex();
    
    // Initial LED Update
    for (let ch = 1; ch <= DDJFLX10.DECKS.COUNT; ch++) {
        DDJFLX10.updateAllPadLeds('[Channel' + ch + ']');
    }
};

// Stop jog ring flash timer
DDJFLX10._stopJogRingFlash = function(deckNum) {
    const timerKey = String(deckNum);
    const timerId = DDJFLX10.jogRingFlashTimers[timerKey];
    if (timerId) {
        engine.stopTimer(timerId);
        delete DDJFLX10.jogRingFlashTimers[timerKey];
    }
};

// Set jog ring mode
DDJFLX10.setJogRing = function(deckNum, mode) {
    const index = deckNum - 1;
    if (index < 0 || index >= DDJFLX10.DECKS.COUNT) {
        return;
    }

    if (mode !== 'flash') {
        DDJFLX10._stopJogRingFlash(deckNum);
    }

    if (mode === 'on') {
        DDJFLX10._outShort(DDJFLX10.MIDI.JOG_DISPLAY_CC, DDJFLX10.JOG_DISPLAY.RING[index], 0x01);
        return;
    }
    if (mode === 'off') {
        DDJFLX10._outShort(DDJFLX10.MIDI.JOG_DISPLAY_CC, DDJFLX10.JOG_DISPLAY.RING[index], 0x00);
        return;
    }

    if (!DDJFLX10.USER_CONFIG.enableJogRingFlash) {
        return;
    }

    // flash mode
    const timerKey = String(deckNum);
    let on = false;
    DDJFLX10._stopJogRingFlash(deckNum);
    DDJFLX10.jogRingFlashTimers[timerKey] = engine.beginTimer(
        DDJFLX10.USER_CONFIG.jogRingFlashIntervalMs,
        function() {
            on = !on;
            DDJFLX10._outShort(DDJFLX10.MIDI.JOG_DISPLAY_CC, DDJFLX10.JOG_DISPLAY.RING[index], on ? 0x01 : 0x00);
        },
        false
    );
};

// Shutdown: Turn off all pad LEDs, jog rings, and displays
DDJFLX10.shutdown = function() {
    if (DDJFLX10.hidIpcTimer) {
        engine.stopTimer(DDJFLX10.hidIpcTimer);
        DDJFLX10.hidIpcTimer = null;
    }
    if (DDJFLX10.sysexKeepaliveTimer) {
        engine.stopTimer(DDJFLX10.sysexKeepaliveTimer);
        DDJFLX10.sysexKeepaliveTimer = null;
    }
    // Stop any jog ring flash timers
    for (let ch = 1; ch <= DDJFLX10.DECKS.COUNT; ch++) {
        DDJFLX10._stopJogRingFlash(ch);
    }

    // Turn off all pad LEDs
    for (let ch = 0; ch < DDJFLX10.DECKS.COUNT; ch++) {
        for (let ctrl = 0x00; ctrl <= 0x7F; ctrl++) {
            DDJFLX10._outShort(DDJFLX10.MIDI.NOTE_ON + ch, ctrl, DDJFLX10.MIDI.LED_OFF);
        }
    }
    
    // Jog rings/displays off
    for (let ch = 1; ch <= DDJFLX10.DECKS.COUNT; ch++) {
        // Turn off Ring
        DDJFLX10._outShort(DDJFLX10.MIDI.JOG_DISPLAY_CC, DDJFLX10.JOG_DISPLAY.RING[ch - 1], DDJFLX10.MIDI.LED_OFF);
        // Hide/Clear Display
        DDJFLX10._outShort(DDJFLX10.MIDI.JOG_DISPLAY_NOTE, DDJFLX10.JOG_DISPLAY.VISIBILITY[ch - 1], 0x7F);  
    }
};