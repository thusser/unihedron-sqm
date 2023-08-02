import datetime
import logging
import threading
import time
from typing import Optional
from astropy.coordinates import EarthLocation, get_sun, AltAz
import astropy.units as u
import serial
from astropy.time import Time


class Report:
    def __init__(self, values: Optional[dict[str, float]] = None, dt: Optional[datetime.datetime] = None):
        self.values = (
            values
            if values is not None
            else {
                "temp_sensor": 0,
                "freq_sensor": 0,
                "ticks_uC": 0,
                "sky_brightness": 0,
            }
        )
        self.time = dt if dt is not None else datetime.datetime.utcnow()


class UnihedronSQM:
    """Class that operates an Unihedron Sky Quality Meter (SQM)."""

    def __init__(
        self,
        port: str = "/dev/ttyUSB0",
        baudrate: int = 4800,
        bytesize: int = 8,
        parity: str = "N",
        stopbits: int = 1,
        rtscts: bool = False,
        timeout: int = 10,
        interval: int = 10,
        location: Optional[tuple[float, float, float]] = None,
        max_sun_alt: float = 10.0,
        *args,
        **kwargs,
    ):
        """

        Args:
            port: Serial port to use.
            baudrate: Baud rate.
            bytesize: Size of bytes.
            parity: Parity.
            stopbits: Stop bits.
            rtscts: RTSCTS.
            *args:
            **kwargs:
        """

        # serial connection
        self._conn = None
        self._port = port
        self._baudrate = baudrate
        self._bytesize = bytesize
        self._parity = parity
        self._stopbits = stopbits
        self._rtscts = rtscts
        self._serial_timeout = timeout

        # stuff
        self.interval = interval

        # location
        self.location = (
            None
            if location is None
            else EarthLocation(lon=location[0] * u.deg, lat=location[1] * u.deg, height=location[2] * u.m)
        )
        self.max_sun_alt = max_sun_alt

        # poll thread
        self._closing = None
        self._thread = None
        self._thread_sleep = 1
        self._max_thread_sleep = 900

        # callback function
        self._callback = None

    def start_polling(self, callback):
        """Start polling the SQM.

        Args:
            callback: Callback function to be called with new data.
        """

        # set callback
        self._callback = callback

        # start thread
        self._closing = threading.Event()
        self._thread = threading.Thread(target=self._poll_thread)
        self._thread.start()

    def stop_polling(self):
        """Stop polling of SQM."""

        # close and wait for thread
        self._closing.set()
        self._thread.join()

    def _poll_thread(self):
        """Thread to poll and respond to the serial output of the SQM.

        The thread places output into a circular list of parsed messages stored as
        dictionaries containing the response itself, the datetime of the response
        and the type of response.  The other methods normally only access the most current report.
        """

        # init
        serial_errors = 0
        sleep_time = self._thread_sleep

        # loop until closing
        while not self._closing.is_set():
            # what about the sun?
            if self.location is not None:
                # get sun location
                sun = get_sun(Time.now()).transform_to(AltAz(location=self.location, obstime=Time.now()))

                # check it
                if sun.alt.degree > self.max_sun_alt:
                    time.sleep(30)
                    continue

            # get serial connection
            if self._conn is None:
                logging.info("connecting to Unihedron SQM sensor")
                try:
                    # connect
                    self._connect_serial()

                    # reset sleep time
                    serial_errors = 0
                    sleep_time = self._thread_sleep

                except serial.SerialException as e:
                    # if no connection, log less often
                    serial_errors += 1
                    if serial_errors % 10 == 0:
                        if sleep_time < self._max_thread_sleep:
                            sleep_time *= 2
                        else:
                            sleep_time = self._thread_sleep

                    # do logging
                    logging.critical("%d failed connections to SQM: %s, sleep %d", serial_errors, str(e), sleep_time)
                    self._closing.wait(sleep_time)

            # actually read next line and process it
            if self._conn is not None:
                # read and analyse data
                data = self.read_data()
                self._callback(Report(data))

            # sleep
            time.sleep(self.interval)

        # close connection
        self._conn.close()

    def _connect_serial(self):
        """Open/reset serial connection to sensor."""

        # close first?
        if self._conn is not None and self._conn.is_open:
            self._conn.close()

        # create serial object
        self._conn = serial.Serial(
            self._port,
            self._baudrate,
            bytesize=self._bytesize,
            parity=self._parity,
            stopbits=self._stopbits,
            timeout=self._serial_timeout,
            rtscts=self._rtscts,
        )

        # open it
        if not self._conn.is_open:
            self._conn.open()

        # initial calls
        time.sleep(1)
        self.read_metadata(tries=10)
        time.sleep(1)
        self.cx_readout = self.read_calibration(tries=10)
        time.sleep(1)
        self.rx_readout = self.read_data(tries=10)

    def read_buffer(self) -> Optional[str]:
        """Read the data"""
        try:
            return self._conn.readline().decode()
        except:
            return None

    def process_metadata(self, msg: str, sep: str = ","):
        # get Photometer identification codes
        s = msg.strip().split(sep)
        protocol_number = int(s[1])
        model_number = int(s[2])
        feature_number = int(s[3])
        serial_number = int(s[4])
        logging.info(
            f"Protocol: {protocol_number}, Model: {model_number}, Feature: {feature_number}, Serial: {serial_number}"
        )

    def read_metadata(self, tries: int = 1):
        """Read the serial number, firmware version"""
        self._conn.write("ix".encode())
        time.sleep(1)
        msg = self.read_buffer()

        # sanity check
        if "i" in msg:
            self.process_metadata(msg)
        elif tries > 0:
            self._connect_serial()
            self.read_metadata(tries - 1)

    def process_calibration(self, msg: str, sep: str = ","):
        # get calibration
        s = msg.strip().split(sep)
        light_calib_offset = float(s[1][:-1])
        dark_calib_period = float(s[2][:-1])
        light_calib_temp = float(s[3][:-1])
        calib_offset = float(s[4][:-1])
        dark_calib_temp = float(s[5][:-1])

        # log
        logging.info("Calibration:")
        logging.info(f"  - Light calibration offset: {light_calib_offset} mag")
        logging.info(f"  - Dark calibration period: {dark_calib_period} s")
        logging.info(f"  - Light calibration temperature: {light_calib_temp} C")
        logging.info(f"  - Calibration offset: {calib_offset} mag")
        logging.info(f"  - Dark calibration temperature: {dark_calib_temp} C")

    def read_calibration(self, tries=1):
        """Read the calibration data"""
        self._conn.write("cx".encode())
        time.sleep(1)
        msg = self.read_buffer()

        # Check caldata
        if "c" in msg:
            self.process_calibration(msg)
        elif tries > 0:
            self._connect_serial()
            self.read_calibration(tries - 1)

    def process_data(self, msg, sep=","):
        # Get the measures
        s = msg.strip().split(sep)
        sky_brightness = float(s[1][:-1])
        freq_sensor = float(s[2][:-2])
        ticks_uC = float(s[3][:-1])
        period_sensor = float(s[4][:-1])
        temp_sensor = float(s[5][:-1])

        # For low frequencies, use the period instead
        if freq_sensor < 30 and period_sensor > 0:
            freq_sensor = 1.0 / period_sensor
        return {
            "temp_sensor": temp_sensor,
            "freq_sensor": freq_sensor,
            "ticks_uC": ticks_uC,
            "sky_brightness": sky_brightness,
        }

    def read_data(self, tries=1):
        """Read the SQM and format the Temperature, Frequency and NSB measures"""
        self._conn.write("rx".encode())
        time.sleep(1)
        msg = self.read_buffer()

        # Check data
        if "r" in msg:
            return self.process_data(msg)
        elif tries > 0:
            self._connect_serial()
            return self.read_data(tries - 1)
        else:
            return None


__all__ = ["UnihedronSQM"]
