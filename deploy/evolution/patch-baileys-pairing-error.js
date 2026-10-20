// Si el pair-success no pasa la validación, Baileys solo lo anota en su log y reintenta: el
// motivo real («Invalid account signature», etc.) se pierde y WhatsApp termina cortando con un
// 401 genérico. Se cierra con 401 y el motivo exacto: Evolution lo guarda en
// Instance.disconnectionObject y Omitel lo lee para la auditoría y la alerta.
const fs = require('fs');

function patch(file, edits) {
  let src = fs.readFileSync(file, 'utf8');
  for (const [before, after] of edits) {
    if (!src.includes(before)) {
      throw new Error(`No se encontró en ${file}:\n${before}`);
    }
    src = src.replace(before, after);
  }
  fs.writeFileSync(file, src);
}

patch('node_modules/baileys/lib/Utils/validate-connection.js', [
  [
    "        throw new boom_1.Boom('Invalid account signature');\n",
    "        throw new boom_1.Boom('Invalid account signature', { data: { accountType } });\n",
  ],
  [
    "        throw new boom_1.Boom('Failed to verify account signature');\n",
    "        throw new boom_1.Boom('Failed to verify account signature', { data: { accountType, deviceType: advDeviceType } });\n",
  ],
]);

patch('node_modules/baileys/lib/Socket/socket.js', [
  [
    "            logger.info({ trace: error.stack }, 'error in pairing');\n            end(error);\n",
    "            logger.info({ trace: error.stack }, 'error in pairing');\n" +
      "            const pairingData = Object.assign({ omitel: 'pairing-error', reason: error.message }, error.data || {});\n" +
      "            console.log('[omitel] pairing-error ' + JSON.stringify(pairingData));\n" +
      '            end(new boom_1.Boom(error.message, { statusCode: 401, data: pairingData }));\n',
  ],
]);
