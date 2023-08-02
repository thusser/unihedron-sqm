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

from lambrecht.influx import Influx
from .lambrecht import Lambrecht


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
            time, values = self.application.current
        elif which == "average":
            time, values = self.application.average
        else:
            raise tornado.web.HTTPError(404)

        # get data
        values["time"] = time

        # send to client
        self.write(json.dumps(values))


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
        self.current: dict[str, float] = {}
        self.buffer: list[tuple[Optional[datetime.datetime], dict[str, float]]] = []
        self.history: list[tuple[Optional[datetime.datetime], dict[str, float]]] = []
        self.log_file = log_file
        self.log_current = log_current
        self.log_average = log_average

        # load history
        self._load_history()

    @property
    def average(self) -> tuple[Optional[datetime.datetime], dict[str, float]]:
        return self.history[0] if len(self.history) > 0 else (None, {})

    def callback(self, time: datetime.datetime, values: dict[str, float]):
        self.current = (time, values)

    def _load_history(self):
        """Load history from log file"""

        # no logfile?
        if self.log_file is None or not os.path.exists(self.log_file):
            return

        # open file
        with open(self.log_file, "r") as csv:
            # check header
            if csv.readline() != "time,skymag\n":
                logging.error("Invalid log file format.")
                return

            # read lines
            for line in csv:
                # split and check
                split = line.split(",")
                if len(split) != 2:
                    logging.error("Invalid log file format.")
                    continue

                # read line
                time = datetime.datetime.strptime(split[0], "%Y-%m-%dT%H:%M:%S")
                values = [float(s) for s in split[1:]]
                self.buffer.append((time, values))

        # crop
        self._crop_history()

    def _crop_history(self):
        # sort history
        self.history = sorted(self.history, key=lambda h: h[0], reverse=True)

        # crop to 10 entries
        if len(self.history) > 10:
            self.history = self.history[:10]

    def sched_callback(self):
        # check
        if len(self.buffer) == 0:
            return

        # average reports
        time = self.buffer[0][0]
        average = {k: np.mean([b[1][k] for b in self.buffer]) for k in self.current.keys()}

        # add to history
        self.history.append((time, average))
        self._crop_history()

        # write to log file?
        if self.log_file is not None:
            # does it exist?
            if not os.path.exists(self.log_file):
                # write header
                with open(self.log_file, "w") as csv:
                    csv.write("time,skymag\n")

            # write line
            with open(self.log_file, "a") as csv:
                fmt = "{time},{skymag.2f}"
                csv.write(fmt.format(time=time.strftime("%Y-%m-%dT%H:%M:%S"), **average))

        # reset reports
        self.buffer.clear()


def main():
    # parser
    parser = argparse.ArgumentParser("Lambrecht meteo data logger")
    parser.add_argument("--http-port", type=int, help="HTTP port for web interface", default=8122)
    parser.add_argument("--port", type=str, help="Serial port to Lambrecht", default="/dev/ttyUSB1")
    parser.add_argument("--baudrate", type=int, help="Baud rate", default=4800)
    parser.add_argument("--bytesize", type=int, help="Byte size", default=8)
    parser.add_argument("--parity", type=str, help="Parity bit", default="N")
    parser.add_argument("--stopbits", type=int, help="Number of stop bits", default=1)
    parser.add_argument("--rtscts", type=bool, help="Use RTSCTS?", default=False)
    parser.add_argument("--log-file", type=str, help="Log file for average values")
    parser.add_argument("--influx", type=str, help="Four strings containing URL, token, org, and bucket", nargs=4)
    args = parser.parse_args()

    # create Lambrecht object
    lambrecht = Lambrecht(**vars(args))

    # init app
    application = Application(**vars(args))

    # influx
    influx = Influx(*args.influx)
    influx.start()

    # callback method
    def callback(time: datetime.datetime, values: dict[str, float]):
        # forward to application and influx
        application.callback(time, values)
        influx(time, values)

    # start polling
    lambrecht.start_polling(callback)

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
    lambrecht.stop_polling()
    sched.shutdown()


if __name__ == "__main__":
    main()
