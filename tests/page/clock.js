const SHIFT = Number(process.env.PAGE_CLOCK_SHIFT_MS || 0);

function shiftedDate(Real, shift) {
  class ShiftedDate extends Real {
    constructor(...args) {
      if (args.length) super(...args);
      else super(Real.now() + shift);
    }
    static now() { return Real.now() + shift; }
  }
  return ShiftedDate;
}

function frozenDate(Real, at) {
  class FrozenDate extends Real {
    constructor(...args) {
      if (args.length) super(...args);
      else super(at);
    }
    static now() { return at; }
  }
  return FrozenDate;
}

if (SHIFT) global.Date = shiftedDate(Date, SHIFT);

module.exports = {SHIFT, shiftedDate, frozenDate};
