// Baileys 6.7.12 se registra sin historySyncConfig en DeviceProps y WhatsApp Business en Android
// rechaza el QR («No se pudo iniciar sesión»). Se agregan los mismos bytes que manda Baileys 6.7.24;
// el proto de 6.7.12 no conoce los campos 9-14, por eso van ya codificados (campo 5 de DeviceProps).
const fs = require('fs');

const file = 'node_modules/baileys/lib/Utils/validate-connection.js';
const before = 'const companionProto = WAProto_1.proto.DeviceProps.encode(companion).finish();';
const historySyncConfig = '2a15188050200130003801400148015001580160017001';
const after =
  'const companionProto = Buffer.concat([WAProto_1.proto.DeviceProps.encode(companion).finish(), ' +
  `Buffer.from('${historySyncConfig}', 'hex')]);`;

const src = fs.readFileSync(file, 'utf8');
if (!src.includes(before)) {
  throw new Error(`No se encontró el registro de DeviceProps en ${file}`);
}
fs.writeFileSync(file, src.replace(before, after));
