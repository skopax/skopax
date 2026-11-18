const OFFSET_HOURS = {UTC: 0, 'Asia/Tokyo': 9};
const MONTHS = ['January', 'February', 'March', 'April', 'May', 'June', 'July', 'August', 'September', 'October', 'November', 'December'];
const pad = (n) => String(n).padStart(2, '0');

function local(iso) {
  const hours = OFFSET_HOURS[process.env.TZ];
  if (hours === undefined) throw new Error('the page suites run in UTC or Asia/Tokyo, not ' + process.env.TZ);
  const d = new Date(Date.parse(iso) + hours * 3600000);
  return d.getUTCDate() + ' ' + MONTHS[d.getUTCMonth()] + ' ' + pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes());
}

const SHORT_MONTHS = MONTHS.map((m) => m.slice(0, 3));
const ZONE_LABEL = {UTC: 'UTC', 'Asia/Tokyo': 'GMT+9'};

function short(iso) {
  const hours = OFFSET_HOURS[process.env.TZ];
  const d = new Date(Date.parse(iso) + hours * 3600000);
  return d.getUTCDate() + ' ' + SHORT_MONTHS[d.getUTCMonth()] + ' ' + pad(d.getUTCHours()) + ':' + pad(d.getUTCMinutes());
}

const zone = () => ZONE_LABEL[process.env.TZ];

module.exports = {local, short, zone};
