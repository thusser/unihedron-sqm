import argparse
import datetime
import json
import os
from typing import Optional
import tornado.ioloop
import tornado.web
import tornado.httpserver
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import logging
import numpy as np

from sqm.influx import Influx
from sqm.sqm import UnihedronSQM, Report

COLUMNS = ["temp_sensor", "freq_sensor", "ticks_uC", "sky_brightness"]


class MainHandler(tornado.web.RequestHandler):
    def get(self):
        app: Application = self.application
        self.render(os.path.join(os.path.dirname(__file__), "template.html"), current=app.current, history=app.history)


class JsonHandler(tornado.web.RequestHandler):
    def set_default_headers(self):
        self.set_header("Content-Type", "application/json")

    def get(self, which):
        """JSON output of data.

        Args:
            which: "current" or "average".

        Returns:
            JSON output.
        """

        # get record
        if which == "current":
            report = self.application.current
        elif which == "average":
            report = self.application.average
        else:
            raise tornado.web.HTTPError(404)

        # send to client
        self.write(json.dumps(report))


class Application(tornado.web.Application):
    def __init__(self, log_file: str = None, log_current: str = None, log_average: str = None, *args, **kwargs):
        # static path
        static_path = os.path.join(os.path.dirname(__file__), "static_html/")

        # init tornado
        tornado.web.Application.__init__(
            self,
            [
                (r"/", MainHandler),
                (r"/(.*).json", JsonHandler),
                (r"/static/(.*)", tornado.web.StaticFileHandler, {"path": static_path}),
            ],
        )

        # init other stuff
        self.current: Report = Report()
        self.buffer: list[Report] = []
        self.history: list[Report] = []
        self.log_file = log_file
        self.log_current = log_current
        self.log_average = log_average

        # load history
        self._load_history()

    @property
    def average(self) -> Report:
        return self.history[0] if len(self.history) > 0 else Report()

    def callback(self, report: Report):
        self.current = report
        self.buffer.append(report)

    def _load_history(self):
        """Load history from log file"""

        # no logfile?
        if self.log_file is None or not os.path.exists(self.log_file):
            return

        # open file
        with open(self.log_file, "r") as csv:
            # check header
            if csv.readline() != f"time,{','.join(COLUMNS)}\n":
                logging.error("Invalid log file format.")
                return

            # read lines
            for line in csv:
                # split and check
                split = line.split(",")
                if len(split) != len(COLUMNS) + 1:
                    logging.error("Invalid log file format.")
                    continue

                # read line
                values = {c: float(s) for c, s in zip(COLUMNS, split[1:])}
                time = datetime.datetime.strptime(split[0], "%Y-%m-%dT%H:%M:%S")
                self.history.append(Report(values, time))

        # crop
        self._crop_history()

    def _crop_history(self):
        # sort history
        self.history = sorted(self.history, key=lambda h: h.time, reverse=True)

        # crop to 10 entries
        if len(self.history) > 10:
            self.history = self.history[:10]

    def sched_callback(self):
        # check
        if len(self.buffer) == 0:
            return

        # average reports
        average = {k: np.mean([b.values[k] for b in self.buffer]) for k in COLUMNS}
        time = datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S")

        # add to history
        self.history.append(Report(average))
        self._crop_history()

        # write to log file?
        if self.log_file is not None:
            # does it exist?
            if not os.path.exists(self.log_file):
                # write header
                with open(self.log_file, "w") as csv:
                    csv.write(f"time,{','.join(COLUMNS)}\n")

            # write line
            with open(self.log_file, "a") as csv:
                fmt = "{time}," + ",".join(["{" + c + ":.2f}" for c in COLUMNS])
                csv.write(fmt.format(time=time, **average))
                csv.write("\n")

        # reset reports
        self.buffer.clear()


def main():
    # logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(filename)s:%(lineno)d %(message)s")

    # parser
    parser = argparse.ArgumentParser("Lambrecht meteo data logger")
    parser.add_argument("--http-port", type=int, help="HTTP port for web interface", default=8122)
    parser.add_argument("--interval", type=int, help="Interval between measurements in secs", default=10)
    parser.add_argument("--port", type=str, help="Serial port to Lambrecht", default="/dev/ttyUSB0")
    parser.add_argument("--baudrate", type=int, help="Baud rate", default=115200)
    parser.add_argument("--bytesize", type=int, help="Byte size", default=8)
    parser.add_argument("--parity", type=str, help="Parity bit", default="N")
    parser.add_argument("--stopbits", type=int, help="Number of stop bits", default=1)
    parser.add_argument("--rtscts", type=bool, help="Use RTSCTS?", default=False)
    parser.add_argument("--log-file", type=str, help="Log file for average values")
    parser.add_argument("--influx", type=str, help="Four strings containing URL, token, org, and bucket", nargs=4)
    args = parser.parse_args()

    # create SQM object
    sqm = UnihedronSQM(**vars(args))

    # init app
    application = Application(**vars(args))

    # influx
    p = [] if args.influx is None else args.influx
    influx = Influx(*p)
    influx.start()

    # callback method
    def callback(report: Report):
        # forward to application and influx
        application.callback(report)
        influx(report)

    # start polling
    sqm.start_polling(callback)

    # init tornado web server
    http_server = tornado.httpserver.HTTPServer(application)
    http_server.listen(args.http_port)

    # scheduler
    sched = BackgroundScheduler()
    trigger = CronTrigger(minute="*/5")
    sched.add_job(application.sched_callback, trigger)
    sched.start()

    # start loop
    try:
        tornado.ioloop.IOLoop.current().start()
    except KeyboardInterrupt:
        pass

    # stop polling
    influx.stop()
    sqm.stop_polling()
    sched.shutdown()


if __name__ == "__main__":
    main()
