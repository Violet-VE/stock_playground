# -*- coding: utf-8 -*-
# @Author: youerning
# @Email: 673125641@qq.com

# import numpy as np
# import pandas as pd
import sys
import numpy as np
from abc import ABC, abstractmethod
from collections import UserDict
from itertools import chain
from datetime import datetime
from pandas.tseries.frequencies import to_offset
from pandas.tseries.offsets import Hour, Day
from .broker import Base as BrokerBase
from .broker import BackTestBroker
from .utils import logger
from .hooks import Stat


class Context(UserDict):
    def __getattr__(self, key):
        # 让调用这可以通过索引或者属性引用皆可
        return self[key]

    def set_currnet_time(self, tick):
        self["now"] = tick

        # list(filter(self.faster_loop, self["feed"].items()))
        tick_data = {}
        # 获取当前所有有报价的股票报价
        # 好像没法更快了
        # loc大概306 µs ± 9.28 µs per loop
        for code, hist in self["feed"].items():
            df = hist[hist.index == tick]
            if len(df) == 1:
                tick_data[code] = df.loc[tick]
                self.latest_price[code] = df.loc[tick][self.broker.deal_price]
            if len(df) > 1:
                sys.exit("历史数据存在重复时间戳！终止运行")

        self["tick_data"] = tick_data

    def faster_loop(self, code_hist):
        tick = self["now"]
        code = code_hist[0]
        hist = code_hist[1]
        tick_data = {}
        df = hist[hist.index == tick]
        if len(df) == 1:
            tick_data[code] = df.loc[tick]
            self.latest_price[code] = df.loc[tick][self.broker.deal_price]
        if len(df) > 1:
            sys.exit("历史数据存在重复时间戳！终止运行")

        self["tick_data"] = tick_data

    def get_hist(self, code=None):
        """如果不指定code, 获取截至到当前时间的所有股票的历史数据"""
        if code is None:
            ret = {}
            for code, hist in self["feed"].items():
                ret[code] = hist[hist.index <= self.now]
            return ret
        elif code in self.feed:
            hist = self.feed[code]
            return {code: hist[hist.index <= self.now]}


class Scheduler(object):
    """
    整个回测过程中的调度中心, 通过一个个时间刻度(tick)来驱动回测逻辑

    所有被调度的对象都会绑定一个叫做ctx的Context对象,由于共享整个回测过程中的所有关键数据,
    可用变量包括:
        ctx.feed: {code1: pd.DataFrame, code2: pd.DataFrame}对象
        ctx.now: 循环所处时间
        ctx.tick_data: 循环所处时间的所有有报价的股票报价
        ctx.trade_cal: 交易日历
        ctx.broker: Broker对象
        ctx.bt/ctx.backtest: Backtest对象

    可用方法:
        ctx.get_hist

    """

    def __init__(self):
        """"""
        self.ctx = Context()
        self._pre_hook_lst = []
        self._post_hook_lst = []
        self._runner_lst = []

    def add_feed(self, feed):
        self.ctx["feed"] = feed

    def add_hook(self, hook, typ="post"):
        if typ == "post" and hook not in self._post_hook_lst:
            self._post_hook_lst.append(hook)
        elif typ == "pre" and hook not in self._pre_hook_lst:
            self._pre_hook_lst.append(hook)

    def add_broker(self, broker):
        self.ctx["broker"] = broker

    def add_backtest(self, backtest):
        self.ctx["backtest"] = backtest
        # 简写
        self.ctx["bt"] = backtest

    def add_runner(self, runner):
        if runner in self._runner_lst:
            return
        self._runner_lst.append(runner)

    def add_trade_cal(self, trade_cal):
        """增加交易日历"""
        self.ctx["trade_cal"] = trade_cal

    def faster_loop(self, tick):
        self.ctx.set_currnet_time(tick)
        for runner in self.loop_run_lst:
            runner.run(tick)

    def run(self):
        # 缓存每只股票的最新价格
        self.ctx["latest_price"] = {}
        # runner指存在可调用的initialize, finish, run(tick)的对象
        runner_lst = list(chain(self._pre_hook_lst, self._runner_lst, self._post_hook_lst))
        # 循环开始前为broker, backtest, hook等实例绑定ctx对象及调用其initialize方法
        for runner in runner_lst:
            runner.ctx = self.ctx
            runner.initialize()

        # 创建交易日历
        if "trade_cal" not in self.ctx:
            df = list(self.ctx.feed.values())[0]
            self.ctx["trade_cal"] = df.index

        # 通过遍历交易日历的时间依次调用runner
        # 首先调用所有pre-hook的run方法
        # 然后调用broker,backtest的run方法
        # 最后调用post-hook的run方法
        # filter 大概比for循环快一倍
        self.loop_run_lst = runner_lst
        list(filter(self.faster_loop, self.ctx.trade_cal))
        # for tick in self.ctx.trade_cal:
        #     self.ctx.set_currnet_time(tick)
        #     for runner in runner_lst:
        #         runner.run(tick)
            # for pre_hook in self._pre_hook_lst:
            #     pre_hook.run(tick)

            # for runner in self._runner_lst:
            #     runner.run(tick)

            # for post_hook in self._post_hook_lst:
            #     post_hook.run(tick)

        # 循环结束后调用所有runner对象的finish方法
        for runner in runner_lst:
            runner.finish()


class BackTest(ABC):
    """
    回测引擎的基类
    提供回测的大致框架, 提供各个事件的钩子函数

    Parameters:
      feed:dict
                股票代码历史数据， 如{"000001.SZ": pd.DataFrame}
      cash:int
                用于股票回测的初始资金
      broker:Broker
                交易平台对象
      trade_cal:list
                以时间戳为元素的序列
      enable_stat:bool
                开启统计功能
      deal_type: str
                成交类型, now代表当前bar提交订单，next_bar代表下一个bar提交订单
    """

    def __init__(self, feed, cash=100000, broker=None, trade_cal=None, enable_stat=True, deal_type="now"):
        self.feed = feed
        self._sch = Scheduler()
        self._logger = logger

        if isinstance(broker, BrokerBase):
            broker = broker
        else:
            broker = BackTestBroker(cash)

        if deal_type == "next_bar":
            # 因为broker对象在backtest对象之前加入scheduler对象，所以broker会在backtest的下一个tick处理backtest提交的订单
            self._sch.add_runner(broker)
            self._sch.add_broker(broker)
            self._sch.add_runner(self)
            self._sch.add_backtest(self)
        elif deal_type == "now":
            self._sch.add_runner(self)
            self._sch.add_backtest(self)
            self._sch.add_runner(broker)
            self._sch.add_broker(broker)

        # self._sch.add_runner(broker)
        # self._sch.add_broker(broker)
        self._sch.add_feed(feed)
        self.stat = Stat()

        if enable_stat:
            self._sch.add_hook(self.stat)

        if trade_cal is not None:
            self._sch.add_trade_cal(trade_cal)

        # self.is_market_start = True
        # 用于判断是否为开市
        self.last_tick = None

        # 历史resample数据的缓存
        self.hist_cache = {}

        # 设置默认freq
        self.default_frq = to_offset(list(feed.values())[0].index.to_series().diff().min())

    def info(self, msg):
        self._logger.info(msg)

    def add_hook(self, *agrs, **kwargs):
        self._sch.add_hook(*agrs, **kwargs)

    def initialize(self):
        """在回测开始前的初始化"""
        pass
    
    def on_market_start(self):
        pass

    def on_market_close(self):
        pass

    def before_on_tick(self, tick):
        pass

    def after_on_tick(self, tick):
        pass

    def before_trade(self, order):
        """在交易之前会调用此函数

        可以在此放置资金管理及风险管理的代码
        如果返回True就允许交易，否则放弃交易
        """
        return True

    def on_order_ok(self, order):
        """当订单执行成功后调用"""
        pass

    def on_order_timeout(self, order):
        """当订单超时后调用"""
        pass

    def finish(self):
        """在回测结束后调用"""
        pass

    def run(self, tick):
        self.current_day = datetime(self.ctx.now.year, self.ctx.now.month, self.ctx.now.day)
        if not self.last_tick:
            self.last_tick = self.ctx.now
            self.on_market_start()

        diff = self.ctx.now - self.last_tick
        # 15:00 - 09:30 = 19800 secs
        if diff.total_seconds() > 19800:
            self.last_tick = self.ctx.now
            self.on_market_start()

        self.before_on_tick(tick)
        self.on_tick(tick)
        self.after_on_tick(tick)
        if self.ctx.now.hour >= 15:
            self.on_market_close()

    def start(self):
        self._sch.run()

    @abstractmethod
    def on_tick(self, tick):
        """
        回测实例必须实现的方法，并编写自己的交易逻辑
        """
        pass
    
    def freq_history(self, code, columns, size, agg_args, freq=None):
        """
        获取feed中历史数据

        Parameters
        ---------
        code : str
            股票代码
        columns : list
            历史数据中需要的字段
        size : int
            历史数量的大小
        frq : str
            时间周期
        
        Returns
        ---------
        """
        # from pandas.tseries.frequencies import to_offset
        # pd.to_timedelta(to_offset('30T'))
        # Timedelta('0 days 00:30:00')
        # agg_args={"open": "first", "high": "max", "low": "min", "close": "last", "vol": "sum"}
        if freq is None:
            df = self.feed[code][columns]
            return df[df.index <= self.ctx.now].tail(size)

        if freq in self.hist_cache and code in self.hist_cache[freq]:
            df = self.hist_cache[freq][code][columns]
            return df[df.index <= self.ctx.now].tail(size)

        if freq not in self.hist_cache:
            self.hist_cache[freq] = {}

        df = self.feed[code][columns]
        df = df.resample(freq).agg(agg_args)
        df = df.replace(0, np.nan)
        df = df.dropna()

        self.hist_cache[freq][code] = df
        return  df[df.index <= self.ctx.now].tail(size)
