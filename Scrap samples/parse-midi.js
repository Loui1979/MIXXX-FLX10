const XLSX = require('xlsx');
const fs = require('fs');

const workbook = XLSX.readFile('FLX10- Midi messages - spreadsheetfed.xlsx');
const sheet = workbook.Sheets[workbook.SheetNames[0]];
const data = XLSX.utils.sheet_to_json(sheet);

const output = "DDJFLX10.midiTable = " + JSON.stringify(data, null, 2) + ";\n";

fs.writeFileSync('midi-table-output.js', output);

console.log("Parsed", data.length, "rows");
console.log("Saved to midi-table-output.js");