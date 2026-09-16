from jesse.strategies import Strategy

import jesse.indicators as ta

from jesse import utils





class CloudScalper(Strategy):

    @property

    def trend(self):

        c = ta.ichimoku_cloud(self.candles)



        if c.conversion_line > c.base_line and c.span_a > c.span_b:

            return 1

        elif c.conversion_line < c.base_line and c.span_a < c.span_b:

            return -1

        else:

            return 0

    

    @property

    def big_candles(self):

        return self.get_candles(self.exchange, self.symbol, '4h')

    

    @property

    def longterm_trend(self):

        return 1 if self.price > ta.ema(self.big_candles, period=200) else -1

    

    @property

    def adx(self):

        return ta.adx(self.candles) > 50

    

    @property

    def bbw(self):

        return ta.bollinger_bands_width(self.candles) * 100 < 5



    def should_long(self) -> bool:

        return self.trend == 1 and self.adx and self.bbw and self.longterm_trend == 1



    def should_short(self) -> bool:

        return self.trend == -1 and self.adx and self.bbw and self.longterm_trend == -1

        

    def go_long(self):

        entry = self.price

        stop = entry - ta.atr(self.candles) * 2.5

        qty = utils.risk_to_qty(self.available_margin, 3, entry, stop, fee_rate=self.fee_rate)

        self.buy = qty*3, entry



    def go_short(self):

        entry = self.price

        stop = entry + ta.atr(self.candles) * 2.5

        qty = utils.risk_to_qty(self.available_margin, 3, entry, stop, fee_rate=self.fee_rate)

        self.sell = qty*3, entry

    

    def on_open_position(self, order):

        if self.is_long:

            self.take_profit = self.position.qty, self.price + ta.atr(self.candles) * 3.2

            self.stop_loss = self.position.qty, self.price - ta.atr(self.candles) * 2.5

        elif self.is_short:

            self.take_profit = self.position.qty, self.price - ta.atr(self.candles) * 3.2

            self.stop_loss = self.position.qty, self.price + ta.atr(self.candles) * 2.5