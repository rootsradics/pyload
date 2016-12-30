# -*- coding: utf-8 -*-

from __future__ import absolute_import
from __future__ import unicode_literals
from __future__ import division
from builtins import str
from past.utils import old_div
from time import time
from threading import RLock

from pyload.api import AccountInfo, ConfigItem
from pyload.network.cookie import CookieJar
from pyload.config.convert import from_string, to_configdata
from pyload.utils import to_string, compare_time, format_size, parse_size , lock

from pyload.plugin import Base


class WrongPassword(Exception):
    pass


#noinspection PyUnresolvedReferences
class Account(Base):
    """
    Base class for every account plugin.
    Just overwrite `login` and cookies will be stored and the account becomes accessible in\
    associated hoster plugin. Plugin should also provide `loadAccountInfo`. \
    An instance of this class is created for every entered account, it holds all \
    fields of AccountInfo ttype, and can be set easily at runtime.
    """

    # constants for special values
    UNKNOWN = -1
    UNLIMITED = -2

    # Default values
    valid = True
    validuntil = -1
    trafficleft = -1
    maxtraffic = -1
    premium = True

    #: after that time [in minutes] pyload will relogin the account
    login_timeout = 600
    #: account data will be reloaded after this time
    info_threshold = 600

    @classmethod
    def from_info_data(cls, m, info, password, options):
        return cls(m, info.aid, info.loginname, info.owner,
                   True if info.activated else False, True if info.shared else False, password, options)

    __type__ = "account"

    def __init__(self, manager, aid, loginname, owner, activated, shared, password, options):
        Base.__init__(self, manager.pyload, owner)

        self.aid = aid
        self.loginname = loginname
        self.owner = owner
        self.activated = activated
        self.shared = shared
        self.password = password
        self.options = options

        self.manager = manager

        self.lock = RLock()
        self.timestamp = 0
        self.login_ts = 0 # timestamp for login
        self.cj = CookieJar()
        self.error = None

        try:
            self.config_data = dict(to_configdata(x) for x in self.__config__)
        except Exception as e:
            self.log_error("Invalid config: %s" % e)
            self.config_data = {}

        self.init()

    def to_info_data(self):
        info = AccountInfo(self.aid, self.__name__, self.loginname, self.owner, self.valid, self.validuntil, self.trafficleft,
                           self.maxtraffic, self.premium, self.activated, self.shared, self.options)

        info.config = [ConfigItem(name, item.label, item.description, item.input,
                                  to_string(self.get_config(name))) for name, item in
                       self.config_data.items()]
        return info

    def init(self):
        pass

    def get_config(self, option):
        """ Gets an option that was configured via the account options dialog and
        is only valid for this specific instance."""
        if option not in self.config_data:
            return Base.get_config(self, option)

        if option in self.options:
            return self.options[option]

        return self.config_data[option].input.default_value

    def set_config(self, option, value):
        """ Sets a config value for this account instance. Modifying the global values is not allowed. """
        if option not in self.config_data:
            return

        value = from_string(value, self.config_data[option].input.type)
        # given value is the default value and does not need to be saved at all
        if value == self.config_data[option].input.default_value:
            if option in self.options:
                del self.options[option]
        else:
            self.options[option] = from_string(value, self.config_data[option].input.type)

    def login(self, req):
        """login into account, the cookies will be saved so the user can be recognized

        :param req: `Request` instance
        """
        raise NotImplementedError

    def relogin(self):
        """ Force a login. """
        req = self.get_account_request()
        try:
            return self._login(req)
        finally:
            req.close()

    @lock
    def _login(self, req):
        # set timestamp for login
        self.login_ts = time()

        try:
            try:
                self.login(req)
            except TypeError: #TODO: temporary
                self.log_debug("Deprecated .login(...) signature omit user, data")
                self.login(self.loginname, {"password": self.password}, req)

            self.valid = True
        except WrongPassword:
            self.log_warning(
                _("Could not login with account %(user)s | %(msg)s") % {"user": self.loginname
                    , "msg": _("Wrong Password")})
            self.valid = False

        except Exception as e:
            self.log_warning(
                _("Could not login with account %(user)s | %(msg)s") % {"user": self.loginname
                    , "msg": e})
            self.valid = False
            self.pyload.print_exc()

        return self.valid

    def restore_defaults(self):
        self.validuntil = Account.validuntil
        self.trafficleft = Account.trafficleft
        self.maxtraffic = Account.maxtraffic
        self.premium = Account.premium

    def set_login(self, loginname, password):
        """ updates the loginname and password and returns true if anything changed """

        if password != self.password or loginname != self.loginname:
            self.login_ts = 0
            self.valid = True #set valid, so the login will be retried

            self.loginname = loginname
            self.password = password
            return True

        return False

    def update_config(self, items):
        """  Updates the accounts options from config items """
        for item in items:
            # Check if a valid option
            if item.name in self.config_data:
                self.set_config(item.name, item.value)

    def get_account_request(self):
        return self.pyload.request.get_request(self.cj)

    def get_download_settings(self):
        """ Can be overwritten to change download settings. Default is no chunkLimit, max dl limit, resumeDownload

        :return: (chunkLimit, limitDL, resumeDownload) / (int, int, bool)
        """
        return -1, 0, True

    # TODO: this method is ambiguous, it returns nothing, but just retrieves the data if needed
    @lock
    def get_account_info(self, force=False):
        """retrieve account info's for an user, do **not** overwrite this method!\\
        just use it to retrieve info's in hoster plugins. see `loadAccountInfo`

        :param name: username
        :param force: reloads cached account information
        """
        if force or self.timestamp + self.info_threshold * 60 < time():

            # make sure to login
            req = self.get_account_request()
            self.check_login(req)
            self.log_info(_("Get Account Info for %s") % self.loginname)
            try:
                try:
                    infos = self.load_account_info(req)
                except TypeError: #TODO: temporary
                    self.log_debug("Deprecated .load_account_info(...) signature, omit user argument.")
                    infos = self.load_account_info(self.loginname, req)
            except Exception as e:
                infos = {"error": str(e)}
                self.log_error(_("Error: %s") % e)
            finally:
                req.close()

            self.restore_defaults() # reset to initial state
            if isinstance(infos, dict): # copy result from dict to class
                for k, v in infos.items():
                    if hasattr(self, k):
                        setattr(self, k, v)
                    else:
                        self.log_debug("Unknown attribute %s=%s" % (k, v))

            self.log_debug("Account Info: %s" % str(infos))
            self.timestamp = time()
            self.pyload.evm.dispatch_event("account:loaded", self.to_info_data())

    #TODO: remove user
    def load_account_info(self, req):
        """ Overwrite this method and set account attributes within this method.

        :param user: Deprecated
        :param req: Request instance
        :return:
        """
        pass

    def get_account_cookies(self, user):
        self.log_debug("Deprecated method .getAccountCookies -> use account.cj")
        return self.cj

    def select_account(self, *args):
        self.log_debug("Deprecated method .selectAccount() -> use fields directly")
        return self.loginname, self.get_account_data()

    def get_account_data(self, *args):
        self.log_debug("Deprecated method .getAccountData -> use fields directly")
        return {"password": self.password, "premium": self.premium, "trafficleft": self.trafficleft,
                "maxtraffic" : self.maxtraffic, "validuntil": self.validuntil}

    def is_premium(self, user=None):
        if user: self.log_debug("Deprecated Argument user for .isPremium()", user)
        return self.premium

    def is_usable(self):
        """Check several constraints to determine if account should be used"""

        if not self.valid or not self.activated: return False

        # TODO: not in ui currently
        if "time" in self.options and self.options["time"]:
            time_data = ""
            try:
                time_data = self.options["time"]
                start, end = time_data.split("-")
                if not compare_time(start.split(":"), end.split(":")):
                    return False
            except Exception:
                self.log_warning(_("Your Time %s has a wrong format, use: 1:22-3:44") % time_data)

        if 0 <= self.validuntil < time():
            return False
        if self.trafficleft is 0:  # test explicitly for 0
            return False

        return True

    def parse_traffic(self, string): #returns kbyte
        return old_div(parse_size (string), 1024)

    def format_trafficleft(self):
        if self.trafficleft is None:
            self.get_account_info(force=True)
        return format_size(self.trafficleft * 1024)

    def wrong_password(self):
        raise WrongPassword

    def empty(self, user=None):
        if user: self.log_debug("Deprecated argument user for .empty()", user)

        self.log_warning(_("Account %s has not enough traffic, checking again in 30min") % self.login)

        self.trafficleft = 0
        self.schedule_refresh(30 * 60)

    def expired(self, user=None):
        if user: self.log_debug("Deprecated argument user for .expired()", user)

        self.log_warning(_("Account %s is expired, checking again in 1h") % user)

        self.validuntil = time() - 1
        self.schedule_refresh(60 * 60)

    def schedule_refresh(self, time=0, force=True):
        """ add a task for refreshing the account info to the scheduler """
        self.log_debug("Scheduled Account refresh for %s in %s seconds." % (self.loginname, time))
        self.pyload.scheduler.add_job(time, self.getAccountInfo, [force])

    @lock
    def check_login(self, req):
        """ checks if the user is still logged in """
        if self.login_ts + self.login_timeout * 60 < time():
            if self.login_ts: # separate from fresh login to have better debug logs
                self.log_debug("Reached login timeout for %s" % self.loginname)
            else:
                self.log_info(_("Login with %s") % self.loginname)

            self._login(req)
            return False

        return True