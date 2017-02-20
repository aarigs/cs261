# -*- coding: utf-8 -*-

import sys
import pytz
from datetime import datetime

import rethinkdb as r
from rethinkdb.errors import RqlRuntimeError, RqlDriverError

from purple import db
from purple.anomalous_trade_finder import AnomalousTradeFinder

tz = pytz.timezone('Europe/London')

# write to screen
def stdout_write(s):
    sys.stdout.write(s)
    sys.stdout.flush()

# reset line to write on same line again
def reset_line():
    sys.stdout.write('\r')
    sys.stdout.flush()


class TradesAnalyser:
    def __init__(self, tradeacc_limit=2500):
        # hold symbols in memory
        self.symbols = []
        self.trades_objs = []
        self.tradecount = 0
        self.tradeacc = 0
        self.anomalies = 0
        self.anomaly_identifier = AnomalousTradeFinder()
        self.tradeacc_limit = tradeacc_limit

        # get last item in db and start holding current id
        last_item = db.session.query(db.TradeModel).order_by(db.TradeModel.id.desc()).first()
        self.current_pk = getattr(last_item, 'id', 0)

    def add(self, t, commit=False):
        # increment current id
        self.current_pk = self.current_pk + 1
        # get symbol from memory or insert into db
        symbol_name = self.get_symbol(t.symbol)

        # use mappings instead of instances for improved performance
        trade = {
            'id': self.current_pk,
            'price': t.price,
            'size': t.size,
            'symbol_name': symbol_name,
            'flagged': False
        }

        self.trades_objs.append(trade)
        self.tradecount = self.tradecount + 1
        self.tradeacc = self.tradeacc + 1

        # inform user
        stdout_write('Trades: {} - ({} anomalies) (Ctrl-C to stop)'.format(self.tradecount, self.anomalies))
        reset_line()

        # flush database at accumulator limit
        if self.tradeacc == self.tradeacc_limit:
            self.save_load()
            if commit:
                db.session.commit()
            else:
                db.session.flush()

        if self.anomaly_identifier.is_anomalous(t):
            self.anomalies = self.anomalies + 1
            self.flag(trade)

    def force_commit(self):
        self.save_load()
        db.session.commit()

    def save_load(self):
        # bulk save for improved performance
        if len(self.trades_objs):
            db.session.bulk_insert_mappings(db.TradeModel, self.trades_objs)
        # reset instance variables
        self.trades_objs = []
        self.tradeacc = 0

    def get_symbol(self, s):
        # try and get from memory
        if not s in self.symbols:
            # query db or create
            symbol = db.SymbolModel.get_or_create(s)
            self.symbols.append(s)
        return s

    def flag(self, trade):
        trade['flagged'] = True
        self.force_commit()
        # insert alert in rethinkdb
        with db.get_reql_connection(db=True) as conn:
            r.table('alerts').insert([{
                    'time': tz.localize(datetime.now()),
                    'trade_pk': trade['id'],
            }]).run(conn, durability='soft')