// Cuentas Business conectadas también a la plataforma de Meta (coexistencia / «hosted») firman
// el pair-success con otros prefijos. Baileys 6.7.12 solo conoce los de cuentas normales: el HMAC
// no cuadra («Invalid account signature»), no se completa el registro y WhatsApp corta con 401.
// Mismos prefijos que Baileys 7 (WA_ADV_HOSTED_ACCOUNT_SIG_PREFIX / WA_ADV_HOSTED_DEVICE_SIG_PREFIX).
const fs = require('fs');

const file = 'node_modules/baileys/lib/Utils/validate-connection.js';
const edits = [
  [
    "    const { details, hmac } = WAProto_1.proto.ADVSignedDeviceIdentityHMAC.decode(deviceIdentityNode.content);\n" +
      '    // check HMAC matches\n' +
      "    const advSign = (0, crypto_2.hmacSign)(details, Buffer.from(advSecretKey, 'base64'));\n",
    "    const { details, hmac, accountType } = WAProto_1.proto.ADVSignedDeviceIdentityHMAC.decode(deviceIdentityNode.content);\n" +
      '    const HOSTED = WAProto_1.proto.ADVEncryptionType.HOSTED;\n' +
      "    console.log('[omitel] pair-success accountType=' + accountType);\n" +
      '    const hmacPrefix = accountType === HOSTED ? Buffer.from([6, 5]) : Buffer.alloc(0);\n' +
      '    // check HMAC matches\n' +
      "    const advSign = (0, crypto_2.hmacSign)(Buffer.concat([hmacPrefix, details]), Buffer.from(advSecretKey, 'base64'));\n",
  ],
  [
    '    const accountMsg = Buffer.concat([Buffer.from([6, 0]), deviceDetails, signedIdentityKey.public]);\n',
    '    const advDeviceType = WAProto_1.proto.ADVDeviceIdentity.decode(deviceDetails).deviceType;\n' +
      "    console.log('[omitel] pair-success deviceType=' + advDeviceType);\n" +
      '    const accountMsg = Buffer.concat([Buffer.from(advDeviceType === HOSTED ? [6, 5] : [6, 0]), deviceDetails, signedIdentityKey.public]);\n',
  ],
  [
    '    const deviceMsg = Buffer.concat([Buffer.from([6, 1]), deviceDetails, signedIdentityKey.public, accountSignatureKey]);\n',
    '    const deviceMsg = Buffer.concat([Buffer.from(advDeviceType === HOSTED ? [6, 6] : [6, 1]), deviceDetails, signedIdentityKey.public, accountSignatureKey]);\n',
  ],
];

let src = fs.readFileSync(file, 'utf8');
for (const [before, after] of edits) {
  if (!src.includes(before)) {
    throw new Error(`No se encontró en ${file}:\n${before}`);
  }
  src = src.replace(before, after);
}
fs.writeFileSync(file, src);
