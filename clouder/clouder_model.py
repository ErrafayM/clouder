# -*- coding: utf-8 -*-
##############################################################################
#
# Author: Yannick Buron
# Copyright 2015, TODAY Clouder SASU
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Lesser General Public License with Attribution
# clause as published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public License with
# Attribution clause along with this program. If not, see
# <http://www.gnu.org/licenses/>.
#
##############################################################################


from openerp import models, fields, api, _
from openerp.exceptions import except_orm
from openerp.addons.connector.session import ConnectorSession
from openerp.addons.connector.queue.job import job

from datetime import datetime, timedelta
import subprocess
import paramiko
import os.path
import string
import errno
import random
import time

from os.path import expanduser

import logging
_logger = logging.getLogger(__name__)

ssh_connections = {}

@job
def connector_enqueue(session, model_name, record_id, func_name, *args, **kargs):
    record = session.env[model_name].browse(record_id)
    return getattr(record, func_name)(*args, **kargs)


class QueueJob(models.Model):

    _inherit = 'queue.job'

    clouder_trace = fields.Text('Clouder Trace')
    res_id = fields.Integer('Res ID')


class ClouderModel(models.AbstractModel):
    """
    Define the clouder.model abstract object, which is inherited by most
    objects in clouder.
    """

    _name = 'clouder.model'

    _log_expiration_days = 30
    _autodeploy = True

    # We create the name field to avoid warning for the constraints
    name = fields.Char('Name', size=64, required=True)
    job_ids = fields.One2many(
        'queue.job', 'res_id',
        domain=lambda self: [('model_name', '=', self._name)],
        auto_join=True, string='Jobs')

    @property
    def email_sysadmin(self):
        """
        Property returning the sysadmin email of the clouder.
        """
        return self.env.ref('clouder.clouder_settings').email_sysadmin

    @property
    def user_partner(self):
        """
        Property returning the full name of the server.
        """
        return self.env['res.partner'].search(
            [('user_ids', 'in', int(self.env.uid))])[0]

    @property
    def archive_path(self):
        """
        Property returning the path where are stored the archives
        in the archive container.
        """
        return '/opt/archives'

    @property
    def services_hostpath(self):
        """
        Property returning the path where are stored the archives
        in the host system.
        """
        return '/opt/services'

    @property
    def home_directory(self):
        """
        Property returning the path to the home directory.
        """
        return expanduser("~")

    @property
    def now_date(self):
        """
        Property returning the actual date.
        """
        now = datetime.now()
        return now.strftime("%Y-%m-%d")

    @property
    def now_hour(self):
        """
        Property returning the actual hour.
        """
        now = datetime.now()
        return now.strftime("%H-%M")

    @property
    def now_hour_regular(self):
        """
        Property returning the actual hour.
        """
        now = datetime.now()
        return now.strftime("%H:%M:%S")

    @property
    def now_bup(self):
        """
        Property returning the actual date, at the bup format.
        """
        now = datetime.now()
        return now.strftime("%Y-%m-%d-%H%M%S")

    @api.one
    @api.constrains('name')
    def _check_config(self):
        """
        Check that we specified the sysadmin email in configuration before
        making any action.
        """
        if not self.env.ref('clouder.clouder_settings').email_sysadmin:
            raise except_orm(
                _('Data error!'),
                _("You need to specify the sysadmin email in configuration"))

    @api.multi
    def enqueue(self, func_name):
        session = ConnectorSession(self.env.cr, self.env.uid,
                           context=self.env.context)
        job_uuid = connector_enqueue.delay(session, self._name, self.id, func_name, description=(func_name + ' - ' + self.name))
        job_ids = self.env['queue.job'].search([('uuid', '=', job_uuid)])
        job_ids.write({'res_id': self.id})

    @api.multi
    def log(self, message):
        """
        Add a message in the logs specified in context.

        :param message: The message which will be logged.
        """
        now = datetime.now()
        message = filter(lambda x: x in string.printable, message)
        _logger.info(message)
        if 'job_uuid' in self.env.context:
            job_ids = self.env['queue.job'].search(
                [('uuid', '=', self.env.context['job_uuid'])])
            for job in job_ids:
                job.clouder_trace = (job.clouder_trace or '') +\
                                    now.strftime('%Y-%m-%d %H:%M:%S') + ' : ' +\
                                    message + '\n'


    @api.multi
    def deploy(self):
        """
        Hook which can be used by inheriting objects to execute actions when
        we create a new record.
        """
        self.purge()
        return

    @api.multi
    def purge(self):
        """
        Hook which can be used by inheriting objects to execute actions when
        we delete a record.
        """
        self.purge_links()
        return

    @api.multi
    def deploy_links(self):
        """
        Force deployment of all links linked to a record.
        """
        if hasattr(self, 'link_ids'):
            for link in self.link_ids:
                link.deploy_()

    @api.multi
    def purge_links(self):
        """
        Force purge of all links linked to a record.
        """
        if hasattr(self, 'link_ids'):
            for link in self.link_ids:
                link.purge_()

    @api.multi
    def reinstall(self):
        """"
        Action which purge then redeploy a record.
        """
        self.enqueue('deploy')

    @api.model
    def create(self, vals):
        """
        Override the default create function to create log, call deploy hook,
        and call unlink if something went wrong.

        :param vals: The values needed to create the record.
        """
        res = super(ClouderModel, self).create(vals)
        res = res.with_context(res.create_log('create'))
        try:
            res.deploy()
        except:
            res.log('===================')
            res.log('FAIL! Reverting...')
            res.log('===================')
            res = res.with_context(nosave=True)
            res.unlink()
            raise
        res.end_log()
        return res

    @api.one
    def unlink(self):
        """
        Override the default unlink function to create log and call purge hook.
        """
        try:
            self.purge()
        except:
            pass
        res = super(ClouderModel, self).unlink()
        # Security to prevent log to write in a removed clouder.log
        if 'logs' in self.env.context \
                and self._name in self.env.context['logs'] \
                and self.id in self.env.context['logs'][self._name]:
            del self.env.context['logs'][self._name][self.id]
        log_ids = self.env['clouder.log'].search(
            [('model', '=', self._name), ('res_id', '=', self.id)])
        log_ids.unlink()
        return res

    @api.multi
    def connect(self, port=False, username=False):
        """
        Method which can be used to get an ssh connection to execute command.

        :param host: The host we need to connect.
        :param port: The port we need to connect.
        :param username: The username we need to connect.
        """

        server = self
        if self._name == 'clouder.container':
            server = self.server_id

        global ssh_connections
        host_fullname = server.name + \
                        (port and ('_' + port) or '') + \
                        (username and ('_' + username) or '')
        if host_fullname not in ssh_connections\
                or not ssh_connections[host_fullname]._transport:

            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())

            ssh_config = paramiko.SSHConfig()
            user_config_file = os.path.expanduser("~/.ssh/config")
            if os.path.exists(user_config_file):
                with open(user_config_file) as f:
                    ssh_config.parse(f)
            user_config = ssh_config.lookup(server.name)

            identityfile = None
            if 'identityfile' in user_config:
                host = user_config['hostname']
                identityfile = user_config['identityfile']
                if not username:
                    username = user_config['user']
                if not port:
                    port = user_config['port']

            if identityfile is None:
                raise except_orm(
                    _('Data error!'),
                    _("It seems Clouder have no record in the ssh config to "
                      "connect to your server.\nMake sure there is a '" + self.name + ""
                      "' record in the ~/.ssh/config of the Clouder system user.\n"
                      "To easily add this record, depending if Clouder try to "
                      "connect to a server or a container, you can click on the "
                      "'reinstall' button of the server record or 'reset key' "
                      "button of the container record you try to access."))

            # Security with latest version of Paramiko
            # https://github.com/clouder-community/clouder/issues/11
            if isinstance(identityfile, list):
                identityfile = identityfile[0]

            # Probably not useful anymore, to remove later
            if not isinstance(identityfile, basestring):
                raise except_orm(
                    _('Data error!'),
                    _("For unknown reason, it seems the variable identityfile in "
                      "the connect ssh function is invalid. Please report "
                      "this message.\n"
                      "Identityfile : " + str(identityfile)
                      + ", type : " + type(identityfile)))

            self.log('connect: ssh ' + (username and username + '@' or '') +
                     server.name + (port and ' -p ' + str(port) or ''))

            try:
                ssh.connect(server.name, port=int(port), username=username,
                        key_filename=os.path.expanduser(identityfile))
            except Exception as inst:
                raise except_orm(
                    _('Connect error!'),
                    _("We were not able to connect to your server. Please make "
                      "sure you add the public key in the authorized_keys file "
                      "of your root user on your server.\n"
                      "If you were trying to connect to a container, a click on "
                      "the 'reset key' button on the container record may resolve "
                      "the problem.\n"
                      "Target : " + server.name + "\n"
                      "Error : " + str(inst)))
            ssh_connections[host_fullname] = ssh
        else:
            ssh = ssh_connections[host_fullname]

        return {'ssh': ssh, 'server': server}

    @api.multi
    def execute(self, cmd, stdin_arg=False, path=False, ssh=False):
        """
        Method which can be used with an ssh connection to execute command.

        :param ssh: The connection we need to use.
        :param cmd: The command we need to execute.
        :param stdin_arg: The command we need to execute in stdin.
        :param path: The path where the command need to be executed.
        """

        res_ssh = self.connect()
        ssh, server = res_ssh['ssh'], res_ssh['server']

        if path:
            self.log('path : ' + path)
            cmd.insert(0, 'cd ' + path + ';')

        if self != server:
            cmd.insert(0, 'docker exec ' + self.name)

        self.log('host : ' + server.name)
        self.log('command : ' + ' '.join(cmd))
        stdin, stdout, stderr = ssh.exec_command(' '.join(cmd))
        if stdin_arg:
            for arg in stdin_arg:
                self.log('command : ' + arg)
                stdin.write(arg)
                stdin.flush()
        stdout_read = stdout.read()
        self.log('stdout : ' + stdout_read)
        self.log('stderr : ' + stderr.read())
        return stdout_read

    @api.multi
    def get(self, source, destination, ssh=False):
        """
        Method which can be used with an ssh connection to transfer files.

        :param ssh: The connection we need to use.
        :param source: The path we need to get the file.
        :param destination: The path we need to send the file.
        """

        host = self.name
        if self._name == 'clouder.container':
            #TODO
            self.insert(0, 'docker exec ' + self.name)
            host = self.server_id.name

        if not ssh:
            ssh = self.connect(host)

        sftp = ssh.open_sftp()
        self.log('get : ' + source + ' to ' + destination)
        sftp.get(source, destination)
        sftp.close()

    @api.multi
    def send(self, source, destination, ssh=False):
        """
        Method which can be used with an ssh connection to transfer files.

        :param ssh: The connection we need to use.
        :param source: The path we need to get the file.
        :param destination: The path we need to send the file.
        """

        res_ssh = self.connect()
        ssh, server = res_ssh['ssh'], res_ssh['server']

        final_destination = destination
        tmp_dir = False
        if self != server:
            tmp_dir = '/tmp/clouder/' + str(time.time())
            server.execute(['mkdir', '-p', tmp_dir])
            destination = tmp_dir + '/file'

        sftp = ssh.open_sftp()
        self.log('send : ' + source + ' to ' + destination)
        sftp.put(source, destination)
        sftp.close()

        if tmp_dir != False:
            server.execute(['cat', destination, '|', 'docker', 'exec', '-i', self.name, 'sh', '-c', "'cat > " + final_destination + "'"])
            server.execute(['rm', '-rf', tmp_dir])

    @api.multi
    def execute_local(self, cmd, path=False, shell=False):
        """
        Method which can be used to execute command on the local system.

        :param cmd: The command we need to execute.
        :param path: The path where the command shall be executed.
        :param shell: Specify if the command shall be executed in shell mode.
        """
        self.log('command : ' + ' '.join(cmd))
        cwd = os.getcwd()
        if path:
            self.log('path : ' + path)
            os.chdir(path)
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, shell=shell)
        out = ''
        for line in proc.stdout:
            out += line
            line = 'stdout : ' + line
            self.log(line)
        os.chdir(cwd)
        return out

    @api.multi
    def exist(self, ssh, path):
        """
        Method which use an ssh connection to check is a file exist.

        :param ssh: The connection we need to use.
        :param path: The path we need to check.
        """
        sftp = ssh.open_sftp()
        try:
            sftp.stat(path)
        except IOError, e:
            if e.errno == errno.ENOENT:
                sftp.close()
                return False
            raise
        else:
            sftp.close()
            return True

    @api.multi
    def local_file_exist(self, localfile):
        """
        Method which check is a file exist on the local system.

        :param localfile: The path to the file we need to check.
        """
        return os.path.isfile(localfile)

    @api.multi
    def local_dir_exist(self, localdir):
        """
        Method which check is a directory exist on the local system.

        :param localdir: The path to the dir we need to check.
        """
        return os.path.isdir(localdir)

    @api.multi
    def execute_write_file(self, localfile, value):
        """
        Method which write in a file on the local system.

        :param localfile: The path to the file we need to write.
        :param value: The value we need to write in the file.
        """
        f = open(localfile, 'a')
        f.write(value)
        f.close()


def generate_random_password(size):
    """
    Method which can be used to generate a random password.

    :param size: The size of the random string we need to generate.
    """
    return ''.join(
        random.choice(string.ascii_uppercase + string.ascii_lowercase
                      + string.digits)
        for _ in range(size))

