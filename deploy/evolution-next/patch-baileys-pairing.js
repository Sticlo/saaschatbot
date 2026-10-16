// Baileys 7.0.0-rc14 todavía no atiende <notification type="companion_reg_refresh">: desde jul-2026
// WhatsApp la manda tras escanear el QR (sobre todo en cuentas Business) para retirar el adv secret
// que anuncia el QR. Sin rotarlo, el celular dice «No se pudo iniciar sesión» y nunca llega
// pair-success. Se rota y se redibuja el mismo ref, como WhatsApp Web (Baileys PR #2765).
// Con el código de vinculación llega un link_code_companion_reg sin datos que reventaba con
// «Invalid buffer» (Baileys PR #2602).
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
    "    ws.on('CB:iq,type:set,pair-device', async (stanza) => {",
    "    let refreshPairingQR;\n    ws.on('CB:iq,type:set,pair-device', async (stanza) => {",
  ],
  [
    '        const advB64 = creds.advSecretKey;\n',
    '        let currentRef;\n' +
      '        const renderPairQR = () => {\n' +
      '            if (!currentRef) {\n' +
      '                return;\n' +
      '            }\n' +
      '            const qr = buildPairingQRData(currentRef, noiseKeyB64, identityKeyB64, creds.advSecretKey, browser);\n' +
      "            ev.emit('connection.update', { qr });\n" +
      '        };\n' +
      '        refreshPairingQR = renderPairQR;\n',
  ],
  [
    "            const ref = refNode.content.toString('utf-8');\n" +
      '            const qr = buildPairingQRData(ref, noiseKeyB64, identityKeyB64, advB64, browser);\n' +
      "            ev.emit('connection.update', { qr });\n",
    "            currentRef = refNode.content.toString('utf-8');\n" + '            renderPairQR();\n',
  ],
  [
    '    // device paired for the first time\n',
    "    ws.on('CB:notification,type:companion_reg_refresh', (node) => {\n" +
      "        if (!getBinaryNodeChild(node, 'companion_reg_refresh') && !getBinaryNodeChild(node, 'pair-device-rotate-qr')) {\n" +
      '            return;\n' +
      '        }\n' +
      '        if (creds.me) {\n' +
      '            return;\n' +
      '        }\n' +
      "        creds.advSecretKey = randomBytes(32).toString('base64');\n" +
      "        ev.emit('creds.update', { advSecretKey: creds.advSecretKey });\n" +
      "        console.log('[omitel] companion_reg_refresh: adv secret rotado, QR redibujado');\n" +
      '        if (refreshPairingQR) {\n' +
      '            refreshPairingQR();\n' +
      '        }\n' +
      '    });\n' +
      '    // device paired for the first time\n',
  ],
]);

patch('node_modules/baileys/lib/Socket/messages-recv.js', [
  [
    "                const linkCodeCompanionReg = getBinaryNodeChild(node, 'link_code_companion_reg');\n",
    "                const linkCodeCompanionReg = getBinaryNodeChild(node, 'link_code_companion_reg');\n" +
      "                if (!getBinaryNodeChildBuffer(linkCodeCompanionReg, 'primary_identity_pub')) {\n" +
      '                    break;\n' +
      '                }\n',
  ],
]);
