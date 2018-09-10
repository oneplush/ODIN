#!/usr/bin/python3
# -*- coding: utf-8 -*-

"""
This module contains everything needed to hunt for subdomains, including collecting certificate
data from Censys.io and crt.sh for a given domain name.

The original crt.sh code is from PaulSec's unofficial crt.sh API. That project can be
found here:

https://github.com/PaulSec/crt.sh
"""

import re
import json
import base64
from time import sleep

import click
import requests
import censys.certificates
from bs4 import BeautifulSoup

from . import helpers


class CertSearcher(object):
    """Class for searching crt.sh and Censys.io for certificates and parsing the results."""
    user_agent = "Mozilla/5.0 (Windows NT 6.1; WOW64; rv:40.0) Gecko/20100101 Firefox/40.1"
    crtsh_base_uri = "https://crt.sh/?q={}&output=json"

    def __init__(self):
        """Everything that should be initiated with a new object goes here."""
        try:
            censys_api_id = helpers.config_section_map("Censys")["api_id"]
            censys_api_secret = helpers.config_section_map("Censys")["api_secret"]
            self.censys_cert_search = censys.certificates.CensysCertificates(api_id=censys_api_id, api_secret=censys_api_secret)
        except censys.base.CensysUnauthorizedException:
            self.censys_cert_search = None
            click.secho("[!] Censys reported your API information is invalid, so Censys searches \
will be skipped.", fg="yellow")
            click.secho("L.. You provided ID %s & Secret %s" % (censys_api_id, censys_api_secret), fg="yellow")
        except Exception as error:
            self.censys_cert_search = None
            click.secho("[!] Did not find a Censys API ID/secret.", fg="yellow")
            click.secho("L.. Details:  {}".format(error), fg="yellow")

    def search_crtsh(self, domain, wildcard=True):
        """Collect certificate information from crt.sh for the target domain name. This returns
        a JSON containing certificate information that includes the issuer, issuer and expiration
        dates, and the name.

        domain -- Domain to search for
        wildcard -- Whether or not to prepend a wildcard to the domain (default: True)

        Return a list of objects, like so:
        {
            "issuer_ca_id": 16418,
            "issuer_name": "C=US, O=Let's Encrypt, CN=Let's Encrypt Authority X3",
            "name_value": "hatch.uber.com",
            "min_cert_id": 325717795,
            "min_entry_timestamp": "2018-02-08T16:47:39.089",
            "not_before": "2018-02-08T15:47:39"
        }
        """
        if wildcard:
            domain = "%25.{}".format(domain)
        req = requests.get(self.crtsh_base_uri.format(domain), headers={"User-Agent": self.user_agent})
        if req.ok:
            try:
                content = req.content.decode("utf-8")
                data = json.loads("[{}]".format(content.replace('}{', '},{')))
                return data
            except:
                pass
        return None

    def search_censys_certificates(self, target):
        """Collect certificate information from Censys for the target domain name. This returns
        a dictionary of certificate information that includes the issuer, subject, and a hash
        Censys uses for the /view/ API calls to fetch additional information.

        A Censys API key is required.
        """
        if self.censys_cert_search is None:
            pass
        else:
            try:
                query = "parsed.names: %s" % target
                results = self.censys_cert_search.search(query, fields=['parsed.names',
                        'parsed.signature_algorithm.name','parsed.signature.self_signed',
                        'parsed.validity.start','parsed.validity.end','parsed.fingerprint_sha256',
                        'parsed.subject_dn','parsed.issuer_dn'])
                return results
            except censys.base.CensysRateLimitExceededException:
                click.secho("\n[!] Censys reports your account has run out of API credits.", fg="red")
                return None
            except Exception as error:
                click.secho("\n[!] Error collecting Censys certificate data for {}.".format(target), fg="red")
                click.secho("L.. Details: {}".format(error), fg="red")
                return None

    def parse_cert_subdomain(self, subject_dn):
        """Accepts the Censys certificate data and parses the individual certificate's domain."""
        if "," in subject_dn:
            pos = subject_dn.find('CN=')+3
        else:
            pos = 3
        tmp = subject_dn[pos:]
        if "," in tmp:
            pos = tmp.find(",")
            tmp = tmp[:pos]
        return tmp

    def filter_subdomains(self, domain, subdomains):
        """Filter out uninteresting domains that may be returned from certificates. These are
        domains unrelated to the true target. For example, a search for blizzard.com on Censys
        can return iran-blizzard.ir, an unwanted and unrelated domain.

        Credit to christophetd for this nice bit of code:

        https://github.com/christophetd/censys-subdomain-finder/blob/master/censys_subdomain_finder.py#L31
        """
        return [ subdomain for subdomain in subdomains if '*' not in subdomain and subdomain.endswith(domain) ]


class SubdomainCollector(object):
    """Class for scraping DNS Dumpster and Netcraft to discover subdomains."""
    dnsdumpster_uri = "https://dnsdumpster.com/"
    netcraft_uri = "http://searchdns.netcraft.com/?host={}"
    netcraft_history_uri = "http://toolbar.netcraft.com/site_report?url={}"

    def __init__(self, webdriver=None):
        """Everything that should be initiated with a new object goes here."""
        self.browser = webdriver

    def check_dns_dumpster(self, domain):
        """Collect subdomains known to DNS Dumpster for the provided domain. This is based on
        PaulSec's unofficial DNS Dumpster API available on GitHub.
        """
        results = {}
        cookies = {}

        requests.packages.urllib3.disable_warnings()
        session = requests.session()
        request = session.get(self.dnsdumpster_uri, verify=False)

        csrf_token = session.cookies['csrftoken']
        cookies['csrftoken'] = session.cookies['csrftoken']
        headers = {"Referer": self.dnsdumpster_uri}
        data = {"csrfmiddlewaretoken": csrf_token, "targetip": domain}

        request = session.post(self.dnsdumpster_uri, cookies=cookies, data=data, headers=headers)

        if request.status_code != 200:
            click.secho("\n[!] There appears to have been an error communicating with DNS Dumpster -- {} \
received!".format(request.status_code), fg="red")

        soup = BeautifulSoup(request.content, "lxml")
        tables = soup.findAll("table")

        results = {}
        results['domain'] = domain
        results['dns_records'] = {}
        results['dns_records']['dns'] = self._retrieve_results(tables[0])
        results['dns_records']['mx'] = self._retrieve_results(tables[1])
        results['dns_records']['txt'] = self._retrieve_txt_record(tables[2])
        results['dns_records']['host'] = self._retrieve_results(tables[3])

        # Try to fetch the network mapping image
        try:
            val = soup.find('img', attrs={'class': 'img-responsive'})['src']
            tmp_url = "{}{}".format(self.dnsdumpster_uri, val)
            image_data = base64.b64encode(requests.get(tmp_url).content)
        except Exception:
            image_data = None
        finally:
            results['image_data'] = image_data
        return results

    def _retrieve_results(self, table):
        """Used by check_dns_dumpster to extract the results from the HTML."""
        results = []
        trs = table.findAll('tr')
        for tr in trs:
            tds = tr.findAll('td')
            pattern_ip = r'([0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3})'
            ip = re.findall(pattern_ip, tds[1].text)[0]
            domain = tds[0].text.replace('\n', '').split(' ')[0]
            header = ' '.join(tds[0].text.replace('\n', '').split(' ')[1:])
            reverse_dns = tds[1].find('span', attrs={}).text

            additional_info = tds[2].text
            country = tds[2].find('span', attrs={}).text
            autonomous_system = additional_info.split(' ')[0]
            provider = ' '.join(additional_info.split(' ')[1:])
            provider = provider.replace(country, '')
            data = {'domain': domain,
                    'ip': ip,
                    'reverse_dns': reverse_dns,
                    'as': autonomous_system,
                    'provider': provider,
                    'country': country,
                    'header': header}
            results.append(data)
        return results

    def _retrieve_txt_record(self, table):
        """Used by check_dns_dumpster to extracts the domain's DNS TXT records."""
        results = []
        for td in table.findAll('td'):
            results.append(td.text)
        return results

    def check_netcraft(self, domain):
        """Collect subdomains known to Netcraft for the provided domain. Netcraft blocks scripted
        requests by requiring cookies and JavaScript for all browser, so Selenium is required.

        This is based on code from the DataSploit project, but updated to work with today's
        Netcraft.
        """
        results = []
        target_dom_name = domain.split(".")

        self.browser.get(self.netcraft_uri.format(domain))
        link_regx = re.compile(r'<a href="http://toolbar.netcraft.com/site_report\?url=(.*)">')
        links_list = link_regx.findall(self.browser.page_source)
        for x in links_list:
            dom_name = x.split("/")[2].split(".")
            if (dom_name[len(dom_name) - 1] == target_dom_name[1]) and \
            (dom_name[len(dom_name) - 2] == target_dom_name[0]):
                results.append(x.split("/")[2])
        num_regex = re.compile('Found (.*) site')
        num_subdomains = num_regex.findall(self.browser.page_source)
        if not num_subdomains:
            num_regex = re.compile('First (.*) sites returned')
            num_subdomains = num_regex.findall(self.browser.page_source)
        if num_subdomains:
            if num_subdomains[0] != str(0):
                num_pages = int(num_subdomains[0]) // 20 + 1
                if num_pages > 1:
                    last_regex = re.compile(
                        '<td align="left">%s.</td><td align="left">\n<a href="(.*)" rel="nofollow">' % (20))
                    last_item = last_regex.findall(self.browser.page_source)[0].split("/")[2]
                    next_page = 21

                    for x in range(2, num_pages):
                        url = "http://searchdns.netcraft.com/?host=%s&last=%s&from=%s&restriction=/site%%20contains" % (domain, last_item, next_page)
                        self.browser.get(url)
                        link_regx = re.compile(
                            r'<a href="http://toolbar.netcraft.com/site_report\?url=(.*)">')
                        links_list = link_regx.findall(self.browser.page_source)
                        for y in links_list:
                            dom_name1 = y.split("/")[2].split(".")
                            if (dom_name1[len(dom_name1) - 1] == target_dom_name[1]) and \
                            (dom_name1[len(dom_name1) - 2] == target_dom_name[0]):
                                results.append(y.split("/")[2])
                        last_item = links_list[len(links_list) - 1].split("/")[2]
                        next_page = 20 * x + 1
            else:
                pass
        return results

    def fetch_netcraft_domain_history(self, domain):
        """Fetch a domain's IP address history from NetCraft."""
        # TODO: See if the "Last Seen" and other data can be easily collected for here
        ip_history = []
        sleep(1)
        self.browser.get(self.netcraft_history_uri.format(domain))
        soup = BeautifulSoup(self.browser.page_source, 'html.parser')
        urls_parsed = soup.findAll('a', href=re.compile(r".*netblock\?q.*"))
        for url in urls_parsed:
            if urls_parsed.index(url) != 0:
                result = [str(url).split('=')[2].split(">")[1].split("<")[0], \
                str(url.parent.findNext('td')).strip("<td>").strip("</td>")]
                ip_history.append(result)
        return ip_history