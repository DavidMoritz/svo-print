import getpass
import json
import os
import configparser
import tempfile
from time import sleep
import subprocess

import boto3
import click
from crontab import CronTab
from pytimeparse.timeparse import timeparse

# TODO: Typically, these should be in other modules, but I'm not sure how that will work with Pyinstaller.
# Try it out so this code is more maintainable.

APP_NAME = "svo-print"
AWS_CONFIG_SECTION = 'AWS'
PRINTER_CONFIG_SECTION = 'PRINTER'

CONFIG_FILE = os.path.join(click.get_app_dir(APP_NAME), 'config.ini')

CLI_WARN = 'yellow'
CLI_ERROR = 'red'
CLI_SUCCESS = 'green'
CLI_INFO = 'blue'


def _get_config():
    if not os.path.exists(click.get_app_dir(APP_NAME)):
        os.makedirs(click.get_app_dir(APP_NAME), exist_ok=True)
    parser = configparser.ConfigParser()
    parser.read(CONFIG_FILE)

    if not parser.has_section(AWS_CONFIG_SECTION):
        parser.add_section(AWS_CONFIG_SECTION)
    if not parser.has_section(PRINTER_CONFIG_SECTION):
        parser.add_section(PRINTER_CONFIG_SECTION)
    return parser


# doing this so the click options can have some defaults if the wizard is run again, or the config file is
# otherwise present when a user tries to run this.
CONFIG = _get_config()


def _get_aws_session():
    session = boto3.Session(
        aws_access_key_id=CONFIG[AWS_CONFIG_SECTION]['access_key'],
        aws_secret_access_key=CONFIG[AWS_CONFIG_SECTION]['secret_access_key'],
        region_name=CONFIG[AWS_CONFIG_SECTION]['region']
    )
    return session


def _get_available_printers():
    lpstat = subprocess.Popen(['lpstat', '-a'], stdout=subprocess.PIPE)
    printers = subprocess.check_output(['cut', '-f1', '-d',  ' '], stdin=lpstat.stdout).split()
    lpstat.wait()
    printers = [str(printer, 'utf-8') for printer in printers]
    return printers


def _generate_config(val_dict):
    cfg = configparser.ConfigParser()

    cfg[AWS_CONFIG_SECTION] = {
        'access_key': val_dict['access_key'],
        'secret_access_key': val_dict['secret_access_key'],
        'region': val_dict['region'],
        'queue_name': val_dict['store_name'],
    }
    cfg[PRINTER_CONFIG_SECTION] = {
        'interval': val_dict['interval'],
        'cmd': '{} run'.format(os.path.abspath(__file__)),
        'printer_name': val_dict['printer_name'],
    }

    with open(CONFIG_FILE, 'w') as config_file:
        cfg.write(config_file)


def _schedule():
    """ Setups up a cron job to make sure this thing is alive. Run this every minute (* * * *) """
    crontab = CronTab(user=getpass.getuser())
    try:
        job = next(crontab.find_comment('print-job'))
    except StopIteration:
        job = crontab.new(comment='print-job')
    job.command = CONFIG[PRINTER_CONFIG_SECTION]['cmd']
    job.minute.every(1)
    crontab.write()


def _print_file(file_to_print):
    """ Send the job to the printer. This assumes Mac or Unix like system where lpr exists."""
    subprocess.check_call(['lpr', '-P', CONFIG[PRINTER_CONFIG_SECTION]['printer_name'], file_to_print])


def _jobs():
    session = _get_aws_session()
    sqs = session.resource('sqs')
    queue = sqs.get_queue_by_name(QueueName=CONFIG[AWS_CONFIG_SECTION]['queue_name'])
    for message in queue.receive_messages():
        try:
            yield json.loads(message.body)
        except Exception:
            # TODO: log the exception somewhere
            pass
        else:
            message.delete()


def _send_jobs_to_printer():
    """ Loops through the queue messages, and attempts to download the pdf object, and send it to the printer. """
    s3 = _get_aws_session().resource('s3')
    for job in _jobs():
        file_to_print = os.path.join(tempfile.gettempdir(), os.path.basename(job['s3_key']))
        s3.Bucket(CONFIG[AWS_CONFIG_SECTION]['s3_bucket']).download_file(job['s3_key'], file_to_print)
        _print_file(file_to_print)


@click.group()
def cli():
    """Commands to send SVO print requests to the network printer"""


@cli.command()
@click.option('--access-key', help='AWS access key', required=True, prompt=True,
              default=CONFIG[AWS_CONFIG_SECTION].get('access_key', ''))
@click.option('--secret-access-key', help='AWS Secret access key', required=True, prompt=True,
              default=CONFIG[AWS_CONFIG_SECTION].get('secret_access_key', ''))
@click.option('--region', help="AWS region", default=CONFIG[AWS_CONFIG_SECTION].get('region', 'us-east-1'), prompt=True)
@click.option('--interval', help='Sets the frequency for polling the job queue', show_default=True, prompt=True,
              default=CONFIG[PRINTER_CONFIG_SECTION].get('interval', '30s')
              )
@click.option('--store-name', help='Name of your store', required=True, prompt=True,
              default=CONFIG[AWS_CONFIG_SECTION].get('queue_name', ''))
@click.option('--printer-name', help='Name of your network printer', required=True, prompt=True,
              default=CONFIG[PRINTER_CONFIG_SECTION].get('printer_name', _get_available_printers()[0]),
              type=click.Choice(_get_available_printers()))
def setup(access_key, secret_access_key, region, interval, store_name, printer_name):
    """Setup the printing application """
    _interval = timeparse(interval)
    if _interval is not None:
        interval = _interval
    config_vals = dict(
        access_key=access_key,
        secret_access_key=secret_access_key,
        region=region,
        interval=interval,
        store_name=store_name,
        printer_name=printer_name,
    )
    _generate_config(config_vals)
    _schedule()


@cli.command()
def run():
    """Poll the SQS queue for jobs, and send them to the printer."""
    cfg = _get_config()
    while True:
        _send_jobs_to_printer()
        sleep(cfg[PRINTER_CONFIG_SECTION].getint('interval'))


if __name__ == '__main__':
    cli()