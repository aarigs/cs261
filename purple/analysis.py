# -*- coding: utf-8 -*-

import sys
import pytz
from datetime import datetime

import rethinkdb as r
from rethinkdb.errors import RqlRuntimeError, RqlDriverError

from purple import db
from purple.realtime import NotificationManager
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
        self.notification_manager = NotificationManager()
        self.symbols = set() # a set has better lookup performance (hashtable)
        self.trades_objs = []
        self.tradecount = 0
        self.tradeacc = 0
        self.anomalies = 0
        self.anomaly_identifier = AnomalousTradeFinder()
        self.tradeacc_limit = tradeacc_limit

        # get last item in db and start holding current id
        last_item = db.session.query(db.TradeModel).order_by(db.TradeModel.id.desc()).first()
        self.current_pk = getattr(last_item, 'id', 0)

        self.notification_manager.add(
            level = 'info',
            message = 'Started analysis',
            datetime = tz.localize(datetime.now())
        )

    def add(self, t, sha1_hash, firstday, commit=False):
        # increment current id
        self.current_pk = self.current_pk + 1
        # get symbol from memory or insert into db
        symbol_name = self.get_symbol(t.symbol)

        # use mappings instead of instances for improved performance
        trade = {
            'id': self.current_pk,
            'price': t.price,
            'bid': t.bid,
            'ask': t.ask,
            'size': t.size,
            'symbol_name': symbol_name,
            'flagged': False,
            #Using default postgres date formatting of m/d/y
            'analysis_date': datetime.now().strftime('%m/%d/%Y'),
            'csv_hash': sha1_hash,
            'datetime': t.time
        }

        self.trades_objs.append(trade)
        self.tradecount = self.tradecount + 1
        self.tradeacc = self.tradeacc + 1

        # If it's a csv file or we're on our firstday, store the trade for future analysis
        if firstday:
            self.anomaly_identifier.add(t, self.current_pk)
        # Otherwise analyse one trade individually
        else:
            anomalies = self.anomaly_identifier.calculate_anomalies_single_trade(t, self.current_pk)
            if anomalies:
                for anomaly in anomalies:
                    self.alert(anomaly)
                    self.flag(anomaly)
                db.session.commit()

        # inform user
        stdout_write('Trades: {} (Ctrl-C to stop)'.format(self.tradecount))
        reset_line()

        # flush database at accumulator limit
        if self.tradeacc == self.tradeacc_limit:
            self.save_load()
            if commit:
                db.session.commit()
            else:
                db.session.flush()

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
            self.symbols.add(s)
        return s

    def flag(self, anomaly):
        #Sometimes our id is a date of the anomaly, rather than a trade id
        if isinstance(anomaly["id"], ( int, long )):
            db.session.query(db.TradeModel).filter_by(id=anomaly["id"]).update({"flagged": True})

    def alert(self, anomaly):
        with db.get_reql_connection(db=True) as conn:
            r.table('alerts').insert([{
                'time': anomaly["time"],
                'trade_pk': anomaly["id"],
                'description': anomaly["description"],
                'reviewed': False,
                'error_code': anomaly["error_code"],
                'severity': anomaly["severity"],
                'symbol': anomaly["symbol"]
            }]).run(conn, durability='soft')

    def alert_stats(self, firstday, csv):
        if firstday or csv:
            anomalies = self.anomaly_identifier.calculate_anomalies_first_day(csv)
        else:
            anomalies = self.anomaly_identifier.calculate_anomalies_end_of_day(datetime.now().strftime('%Y-%m-%d'))

        for anomaly in anomalies:
            self.alert(anomaly)
            self.flag(anomaly)

        db.session.commit()

        print 'Found ' + str(len(anomalies)) + ' anomalies'
