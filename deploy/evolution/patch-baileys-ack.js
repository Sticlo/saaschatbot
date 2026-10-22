// Baileys 6.7.12 arma el ack de los mensajes sin «from» (y con «type» solo a veces). WhatsApp lo
// rechaza con un stream:error y corta la conexión: un corte de ~1 s cada 50 minutos por negocio.
// Se arma como WhatsApp Web, igual que Baileys desde el cambio #2373 (src/Utils/stanza-ack.ts).
// Además, una notificación «encrypt» sin <count> tumbaba el manejo de la notificación.
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

patch('node_modules/baileys/lib/Socket/messages-recv.js', [
  [
    "        if (!!attrs.type && (tag !== 'message' || (0, WABinary_1.getBinaryNodeChild)({ tag, attrs, content }, 'unavailable') || errorCode !== 0)) {\n" +
      '            stanza.attrs.type = attrs.type;\n' +
      '        }\n' +
      "        if (tag === 'message' && (0, WABinary_1.getBinaryNodeChild)({ tag, attrs, content }, 'unavailable')) {\n" +
      '            stanza.attrs.from = authState.creds.me.id;\n' +
      '        }\n',
    '        if (!!attrs.type) {\n' +
      '            stanza.attrs.type = attrs.type;\n' +
      '        }\n' +
      "        if (tag === 'message' && authState.creds.me?.id) {\n" +
      '            stanza.attrs.from = authState.creds.me.id;\n' +
      '        }\n',
  ],
  [
    "            const countChild = (0, WABinary_1.getBinaryNodeChild)(node, 'count');\n" +
      '            const count = +countChild.attrs.value;\n',
    "            const countChild = (0, WABinary_1.getBinaryNodeChild)(node, 'count');\n" +
      '            if (!countChild) {\n' +
      '                return;\n' +
      '            }\n' +
      '            const count = +countChild.attrs.value;\n',
  ],
]);
