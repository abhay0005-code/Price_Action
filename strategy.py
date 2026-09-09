import pandas as pd
import numpy as np

class PriceActionStrategy:
    def __init__(self, timeframe='5m', fast_ema=20, slow_ema=50, pivot_left=3, pivot_right=3):
        self.timeframe=timeframe; self.fast_ema=fast_ema; self.slow_ema=slow_ema
        self.pivot_left=pivot_left; self.pivot_right=pivot_right

    def calculate_indicators(self, df):
        df=df.copy().sort_values('timestamp').reset_index(drop=True)
        df['ema_fast']=df.close.ewm(span=self.fast_ema,adjust=False).mean()
        df['ema_slow']=df.close.ewm(span=self.slow_ema,adjust=False).mean()
        df['prev_high']=df.high.shift(1); df['prev_low']=df.low.shift(1)
        ph=np.full(len(df),np.nan); pl=np.full(len(df),np.nan); L=self.pivot_left; R=self.pivot_right
        for i in range(L,len(df)-R):
            wh=df.high.iloc[i-L:i+R+1]; wl=df.low.iloc[i-L:i+R+1]
            if df.high.iloc[i]==wh.max() and (wh==df.high.iloc[i]).sum()==1: ph[i]=df.high.iloc[i]
            if df.low.iloc[i]==wl.min() and (wl==df.low.iloc[i]).sum()==1: pl[i]=df.low.iloc[i]
        df['confirmed_pivot_high']=pd.Series(ph).shift(R)
        df['confirmed_pivot_low']=pd.Series(pl).shift(R)
        df['resistance']=df.confirmed_pivot_high.ffill(); df['support']=df.confirmed_pivot_low.ffill()
        return df

    def generate_signal(self, df):
        df=self.calculate_indicators(df)
        df['trend']=np.where(df.ema_fast>df.ema_slow,'UPTREND',np.where(df.ema_fast<df.ema_slow,'DOWNTREND','SIDEWAYS'))
        df['signal']='HOLD'; df['reason']=''; df['position']='FLAT'
        pos='FLAT'
        for i in range(1,len(df)):
            r=df.iloc[i]; p=df.iloc[i-1]
            if pd.notna(p.high) and r.close>p.high:
                if pos=='LONG': reason='CONTINUE LONG - CLOSE ABOVE PREVIOUS HIGH'
                elif pos=='SHORT': reason='REVERSE TO LONG - CLOSE ABOVE PREVIOUS HIGH'
                else: reason='NEW LONG - CLOSE ABOVE PREVIOUS HIGH'
                signal='BUY'; pos='LONG'
            elif pd.notna(p.low) and r.close<p.low:
                if pos=='SHORT': reason='CONTINUE SHORT - CLOSE BELOW PREVIOUS LOW'
                elif pos=='LONG': reason='REVERSE TO SHORT - CLOSE BELOW PREVIOUS LOW'
                else: reason='NEW SHORT - CLOSE BELOW PREVIOUS LOW'
                signal='SELL'; pos='SHORT'
            else:
                signal='HOLD'; reason=''
            df.at[i,'signal']=signal; df.at[i,'reason']=reason; df.at[i,'position']=pos
        return df

    def latest_signal(self,df):
        r=self.generate_signal(df).iloc[-1]
        f=lambda x: float(x) if pd.notna(x) else float('nan')
        return {'timeframe':self.timeframe,'price':f(r.close),'trend':r.trend,'support':f(r.support),'resistance':f(r.resistance),'signal':r.signal,'reason':r.reason,'position':r.position,'ema_fast':f(r.ema_fast),'ema_slow':f(r.ema_slow)}
