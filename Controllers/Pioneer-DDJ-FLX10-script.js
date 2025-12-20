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
    enableJogTime: true,
    enableJogDisplay: true,
    enableJogRingFlash: true,
    
    // Performance settings
    quickJumpSize: 16, // Beats for quick jump
    jogRingFlashIntervalMs: 300,
    
    // Debug settings
    debugPadInput: false,      // Log pad input events
    debugJogDisplay: false,    // Log jog display updates
    debugMidi: false          // Log all MIDI messages
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
// Key map (Mixxx 0-23 to Pioneer values)
DDJFLX10.PIONEER_KEY_MAP = [
    0x01, 0x03, 0x05, 0x07, 0x09, 0x0B, 0x0D, 0x0F,
    0x11, 0x13, 0x15, 0x17, 0x02, 0x04, 0x06, 0x08,
    0x0A, 0x0C, 0x0E, 0x10, 0x12, 0x14, 0x16, 0x18
];

// Tempo ranges
DDJFLX10.TEMPO_RANGES = [0.06, 0.10, 0.16, 0.25];

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
    
    // Keylock
    KEYLOCK: [0x20, 0x21, 0x22, 0x23],   // Deck 1-4 (Note Status 0x9F)
    
    // Ring/Visibility
    RING: [0x50, 0x51, 0x52, 0x53],      // Deck 1-4 Jog Ring/LED
    VISIBILITY: [0x54, 0x55, 0x56, 0x57], // Deck 1-4 Display Clear/Visibility
    
    // Jog display data (Pioneer PDF)
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
    midi.sendShortMsg(DDJFLX10.MIDI.JOG_DISPLAY_CC, msbControls[index], msb);
    midi.sendShortMsg(DDJFLX10.MIDI.JOG_DISPLAY_CC, lsbControls[index], lsb);
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
    
    // Only handle press events
    if (!padInfo.pressed) {
        if (DDJFLX10.USER_CONFIG.debugPadInput) {
            DDJFLX10.debug('pad', `Release ignored`);
        }
        return;
    }
    
    const group = `[Channel${padInfo.deck}]`;
    
    // Calculate hotcue number: (page-1)*8 + pad = 1-16
    const hotcueNum = ((padInfo.page - 1) * 8) + padInfo.pad;
    
    // Handle based on mode
    switch (padInfo.mode) {
        case 'hotcue':
            if (padInfo.shifted) {
                DDJFLX10.pulseControl(group, `hotcue_${hotcueNum}_clear`);
            } else {
                DDJFLX10.pulseControl(group, `hotcue_${hotcueNum}_activate`);
            }
            break;
            
        case 'padfx':
            // Pad FX: use effect controls
            const fxNum = ((padInfo.page - 1) * 8) + padInfo.pad;
            // TODO: Map to actual effect controls
            break;
            
        default:
            console.warn(`[PAD] Unknown mode: ${padInfo.mode}`);
    }
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
    midi.sendShortMsg(msg.status, msg.data1, colorValue);
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
    midi.sendShortMsg(msg.status, msg.data1, colorValue);
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
    midi.sendShortMsg(DDJFLX10.MIDI.JOG_DISPLAY_CC, DDJFLX10.JOG_DISPLAY.BPM_MSB[index], msb);
    midi.sendShortMsg(DDJFLX10.MIDI.JOG_DISPLAY_CC, DDJFLX10.JOG_DISPLAY.BPM_LSB[index], lsb);
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
    midi.sendShortMsg(DDJFLX10.MIDI.JOG_DISPLAY_CC, DDJFLX10.JOG_DISPLAY.SPEED_MSB[index], msb);
    midi.sendShortMsg(DDJFLX10.MIDI.JOG_DISPLAY_CC, DDJFLX10.JOG_DISPLAY.SPEED_LSB[index], lsb);
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
    midi.sendShortMsg(DDJFLX10.MIDI.JOG_DISPLAY_CC, DDJFLX10.JOG_DISPLAY.TIME_MIN[index], min);
    midi.sendShortMsg(DDJFLX10.MIDI.JOG_DISPLAY_CC, DDJFLX10.JOG_DISPLAY.TIME_SEC[index], sec);
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
    
    // Send duration to jog display (Channel 16 CC)
    // Note: Check if your controller has separate duration displays
    // For now, we'll send to unused controls or update based on your controller's spec
    // This might need adjustment based on your controller's actual MIDI mapping
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

        // Ensure jog info is visible
        midi.sendShortMsg(DDJFLX10.MIDI.JOG_DISPLAY_NOTE, 0x5D + ch, 0x00);

        // Connect playposition marker (degrees)
        engine.makeConnection(group, 'playposition', DDJFLX10.track_marker);

        // Connect BPM display
        engine.makeConnection(group, 'bpm', DDJFLX10.track_bpm);

        // Connect playing speed (rate %)
        engine.makeConnection(group, 'rate', DDJFLX10.track_playing_speed);

        // Connect time display
        engine.makeConnection(group, 'playposition', DDJFLX10.updateJogTime);
        
        // Connect duration display
        engine.makeConnection(group, 'duration', DDJFLX10.updateJogDuration);

        // Trigger initial values once
        DDJFLX10.track_marker(null, group, 'playposition');
        DDJFLX10.track_bpm(null, group, 'bpm');
        DDJFLX10.track_playing_speed(null, group, 'rate');
        DDJFLX10.updateJogTime(null, group, 'playposition');
        DDJFLX10.updateJogDuration(null, group, 'duration');
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
                midi.sendShortMsg(status, 0x02, level); 
            }
        };
        
        // Fix closure issue by storing channel index
        vuOptions.channelIndex = ch - 1;
        
        // VU meter components don't need to be added to containers
        // They work with direct engine connections
        let vuComponent = new components.Component(vuOptions);
        DDJFLX10.vuMeters[ch] = vuComponent;
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
        DDJFLX10.channelContainers[ch] = new components.ComponentContainer({group: group});
        DDJFLX10.padContainers[ch] = new components.ComponentContainer({group: group});
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
        midi.sendShortMsg(DDJFLX10.MIDI.JOG_DISPLAY_NOTE, 0x09 + index, 0x01);
        return;
    }
    if (mode === 'off') {
        midi.sendShortMsg(DDJFLX10.MIDI.JOG_DISPLAY_NOTE, 0x09 + index, 0x00);
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
            midi.sendShortMsg(DDJFLX10.MIDI.JOG_DISPLAY_NOTE, 0x09 + index, on ? 0x01 : 0x00);
        },
        false
    );
};

// Shutdown: Turn off all pad LEDs, jog rings, and displays
DDJFLX10.shutdown = function() {
    // Stop any jog ring flash timers
    for (let ch = 1; ch <= DDJFLX10.DECKS.COUNT; ch++) {
        DDJFLX10._stopJogRingFlash(ch);
    }

    // Turn off all pad LEDs
    for (let ch = 0; ch < DDJFLX10.DECKS.COUNT; ch++) {
        for (let ctrl = 0x00; ctrl <= 0x7F; ctrl++) {
            midi.sendShortMsg(DDJFLX10.MIDI.NOTE_ON + ch, ctrl, DDJFLX10.MIDI.LED_OFF);
        }
    }
    
    // Jog rings/displays off
    for (let ch = 1; ch <= DDJFLX10.DECKS.COUNT; ch++) {
        // Turn off Ring
        midi.sendShortMsg(DDJFLX10.MIDI.JOG_DISPLAY_CC, DDJFLX10.JOG_DISPLAY.RING[ch - 1], DDJFLX10.MIDI.LED_OFF);
        // Hide/Clear Display
        midi.sendShortMsg(DDJFLX10.MIDI.JOG_DISPLAY_NOTE, DDJFLX10.JOG_DISPLAY.VISIBILITY[ch - 1], 0x7F);  
    }
};
