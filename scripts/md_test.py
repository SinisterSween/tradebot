from ib_insync import IB, Contract, Stock

def test_spy_smart():
    ib = IB(); ib.connect('127.0.0.1', 7497, clientId=9001)
    mdc = Contract(conId=756733, exchange='SMART')  # SPY consolidated
    t = ib.reqMktData(mdc, '', False, False)
    ib.sleep(2.0)
    ib.cancelMktData(mdc)
    ib.disconnect()

def test_aapl_smart():
    ib = IB(); ib.connect('127.0.0.1', 7497, clientId=9002)
    mdc = Stock('AAPL', 'SMART', 'USD')             # AAPL consolidated
    t = ib.reqMktData(mdc, '', False, False)
    ib.sleep(2.0)
    ib.cancelMktData(mdc)
    ib.disconnect()

if __name__ == "__main__":
    test_spy_smart()
    test_aapl_smart()
