// Desde jul-2026 WhatsApp manda <notification type="companion_reg_refresh"> tras escanear el QR
// (sobre todo en cuentas Business): retira el adv secret que anuncia el QR. Baileys 6.7.12 la
// descarta, el celular dice «No se pudo iniciar sesión» y nunca llega pair-success. Se rota el
// secreto y se redibuja el mismo ref, como WhatsApp Web (Baileys PR #2765).
// Además, con el código de vinculación llega un link_code_companion_reg sin datos que reventaba
// con «Invalid buffer» (Baileys PR #2602).
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

patch('node_modules/baileys/lib/Socket/socket.js', [
  [
    "    // QR gen\n    ws.on('CB:iq,type:set,pair-device', async (stanza) => {",
    "    let refreshPairingQR;\n    // QR gen\n    ws.on('CB:iq,type:set,pair-device', async (stanza) => {",
  ],
  [
    '        const advB64 = creds.advSecretKey;\n',
    "        let currentRef;\n" +
      "        const renderPairQR = () => {\n" +
      "            if (!currentRef) {\n" +
      "                return;\n" +
      "            }\n" +
      "            const qr = [currentRef, noiseKeyB64, identityKeyB64, creds.advSecretKey].join(',');\n" +
      "            ev.emit('connection.update', { qr });\n" +
      "        };\n" +
      "        refreshPairingQR = renderPairQR;\n",
  ],
  [
    "            const ref = refNode.content.toString('utf-8');\n" +
      "            const qr = [ref, noiseKeyB64, identityKeyB64, advB64].join(',');\n" +
      "            ev.emit('connection.update', { qr });\n",
    "            currentRef = refNode.content.toString('utf-8');\n" + '            renderPairQR();\n',
  ],
  [
    '    // device paired for the first time\n',
    "    ws.on('CB:notification,type:companion_reg_refresh', (node) => {\n" +
      "        if (!(0, WABinary_1.getBinaryNodeChild)(node, 'companion_reg_refresh') &&\n" +
      "            !(0, WABinary_1.getBinaryNodeChild)(node, 'pair-device-rotate-qr')) {\n" +
      "            return;\n" +
      "        }\n" +
      "        if (creds.me) {\n" +
      "            return;\n" +
      "        }\n" +
      "        creds.advSecretKey = require('crypto').randomBytes(32).toString('base64');\n" +
      "        ev.emit('creds.update', { advSecretKey: creds.advSecretKey });\n" +
      "        console.log('[omitel] companion_reg_refresh: adv secret rotado, QR redibujado');\n" +
      "        if (refreshPairingQR) {\n" +
      "            refreshPairingQR();\n" +
      "        }\n" +
      "    });\n" +
      '    // device paired for the first time\n',
  ],
]);

patch('node_modules/baileys/lib/Socket/messages-recv.js', [
  [
    "                const linkCodeCompanionReg = (0, WABinary_1.getBinaryNodeChild)(node, 'link_code_companion_reg');\n",
    "                const linkCodeCompanionReg = (0, WABinary_1.getBinaryNodeChild)(node, 'link_code_companion_reg');\n" +
      "                if (!(0, WABinary_1.getBinaryNodeChildBuffer)(linkCodeCompanionReg, 'primary_identity_pub')) {\n" +
      '                    break;\n' +
      '                }\n',
  ],
]);
