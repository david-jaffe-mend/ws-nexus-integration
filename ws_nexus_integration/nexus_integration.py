import base64
import json
import logging
import os
import re
import sys
from configparser import ConfigParser
from distutils.util import strtobool
from multiprocessing import Pool, Manager
from typing import Union, List, Tuple
from urllib.parse import urlparse, urljoin

import ws_sdk.ws_errors

from ws_nexus_integration._version import __version__, __tool_name__
import requests
from ws_sdk import WS, ws_constants

is_debug = logging.DEBUG if bool(os.environ.get("DEBUG", 0)) else logging.INFO
logger = logging.getLogger(__tool_name__)
logger.setLevel(is_debug)
formatter = logging.Formatter('%(levelname)s %(asctime)s %(thread)d %(name)s: %(message)s')
s_handler = logging.StreamHandler()
s_handler.setFormatter(formatter)
logger.addHandler(s_handler)
# logger.propagate = False

SUPPORTED_FORMATS = {'maven2', 'npm', 'pypi', 'rubygems', 'nuget', 'raw', 'docker'}
DOCKER_TIMEOUT = 600
VER_3_26 = ["3", "26"]

config = None


class Config:
    # @dataclass
    # class Config:
    #     nexus_base_url: str
    #     nexus_alt_docker_registry_address: str
    #     nexus_user: str
    #     nexus_password: str
    #     nexus_auth_token: str
    #     nexus_repos: str
    #     headers: dict

    def __init__(self, conf: dict) -> str:
        def convert_to_basic_string(user_name: str, password: str):
            auth_string_plain = f"{user_name}:{password}"
            basic_bytes = base64.b64encode(bytes(auth_string_plain, "utf-8"))
            basic_string = str(basic_bytes)[2:-1]

            return basic_string

        def get_nexus_auth_token(nexus_user: str, nexus_password: str) -> str:
            nexus_auth_token = conf.get('Nexus Settings', 'NexusAuthToken', fallback=None)
            if nexus_auth_token:
                logger.debug(f"Using Nexus authentication token")
            else:
                logger.debug('Converting user and password to basic string')
                try:
                    nexus_auth_token = convert_to_basic_string(nexus_user, nexus_password)
                except KeyError:
                    logger.error("Nexus username or password are missing from the configuration file")
                    sys.exit(1)

            return nexus_auth_token

        def generate_dirs():
            for k, v in self.__dict__.items():
                if k.endswith("_dir") and not os.path.exists(v):
                    logger.debug(f"Directory {v} does not exist and will be created")
                    os.mkdir(v)

        def set_lang_include(includes: str):
            inc_l = includes.split(',') if len(includes) else None
            if inc_l:
                ret_l = []
                for i in inc_l:
                    ret_l += ws_constants.LibMetaData.LangSuffix.__dict__[i]
                self.ws_conn.ua_conf.set_include_suffices_to_scan(ret_l)

        # Nexus Settings
        self.nexus_base_url = conf.get('Nexus Settings', 'NexusBaseUrl', fallback='http://localhost:8081').strip('/')
        self.nexus_alt_docker_registry_address = conf.get('Nexus Settings', 'NexusAltDockerRegistryAddress', fallback=None)
        self.nexus_user = conf.get('Nexus Settings', 'NexusUser', fallback=None)
        self.nexus_password = conf['Nexus Settings']['NexusPassword']
        self.nexus_auth_token = get_nexus_auth_token(self.nexus_user, self.nexus_password)
        self.nexus_repos = conf.get('Nexus Settings', 'NexusRepositories')
        if self.nexus_repos:
            self.defined_nexus_repo_l = [repo.strip() for repo in self.nexus_repos.split(',')]
        self.nexus_exc_repos = conf['Nexus Settings'].get('NexusExcludedRepositories')
        if self.nexus_exc_repos:
            self.nexus_exc_repos_l = [repo.strip() for repo in self.nexus_exc_repos.split(',')]
        else:
            self.nexus_exc_repos_l = []
        self.headers = {'Authorization': f'Basic {self.nexus_auth_token}',
                        'accept': 'application/json'}
        # WhiteSource Settings
        self.product_name = conf.get('WhiteSource Settings', 'WSProductName', fallback='Nexus')
        self.check_policies = conf.getboolean('WhiteSource Settings', 'WSCheckPolicies', fallback=False)
        self.policies = 'true' if self.check_policies else 'false'
        ws_name = f"ws-{__tool_name__.replace('_', '-')}"
        base_dir = conf.get('General Settings', 'WorkDir')
        if not base_dir:
            base_dir = f"c:/tmp/ws-{ws_name}" if sys.platform == "win32" else f"/tmp/{ws_name}"
        self.base_dir = base_dir
        self.is_docker_scan = False
        self.scan_dir = os.path.join(self.base_dir, '_wstemp')
        java_bin = conf.get('General Settings', 'JavaBin', fallback="java")
        self.ws_conn = WS(user_key=conf['WhiteSource Settings']['WSUserKey'],
                          token=conf['WhiteSource Settings']['WSApiKey'],
                          url=conf.get('WhiteSource Settings', 'WSUrl'),
                          java_bin=java_bin if java_bin else "java",
                          ua_path=self.base_dir,
                          tool_details=(f"ps-{__tool_name__.replace('_', '-')}", __version__))
        set_lang_include(conf['WhiteSource Settings'].get('WSLang', "").replace(" ", ""))

        # General Settings
        self.threads_number = conf.getint('General Settings', 'ThreadCount', fallback=5)
        generate_dirs()


def set_nexus_resources_url(full_version: str) -> str:
    ver = full_version.strip("Nexus/ (OSS)").split(".")
    if ver[0] < VER_3_26[0] or (ver[0] == VER_3_26[0] and ver[1] < VER_3_26[1]):
        resources_url = "/service/rest/beta/repositories"
    else:
        resources_url = "/service/rest/v1/repositorySettings"
    logger.debug(f"Using repository: {resources_url}")

    return resources_url


def retrieve_nexus_repositories() -> List[str]:
    def get_nexus_ver(nexus_version):
        if nexus_version:
            logger.info(f"Nexus Version: {nexus_version}")
        else:
            logger.warning("Server headers does not contain Nexus version. Assuming >=3.26")
            nexus_version = "3.26"

        return nexus_version

    def get_valid_repositories(repos) -> List[str]:
        valid_repos = []
        for repo in repos:
            repo_format = repo.get("format")
            if repo_format in SUPPORTED_FORMATS:
                repo_name = repo["name"]
                valid_repos.append(repo_name)
            else:
                logger.warning(f"Repository: {repo['name']} is unsupported format: {repo_format}. Skipping")

        return valid_repos

    logger.debug("Sending request for retrieving Nexus repository list")
    repositories, resp_headers = call_nexus_api("/service/rest/v1/repositories", include_resp_headers=True)
    config.nexus_version = get_nexus_ver(resp_headers.get('Server'))
    existing_nexus_repository_list = get_valid_repositories(repositories)

    return existing_nexus_repository_list


def get_items_from_repo(repo_name: str) -> List[dict]:
    logger.info(f'Handling repository: {repo_name}')
    repo_comp_url = f'/service/rest/v1/components?repository={repo_name}'

    all_repo_items = []
    continuation_token = None

    while True:
        cur_repo_comp_url = repo_comp_url
        if continuation_token is not None:
            cur_repo_comp_url += f"&continuationToken={continuation_token}"
        cur_comp_resp = call_nexus_api(cur_repo_comp_url)
        continuation_token = None

        if isinstance(cur_comp_resp, dict):     # TODO: RECONSIDER REMOVING AS THIS SHOULDN'T HAPPEN
            all_repo_items.extend(cur_comp_resp.get('items', []))
            continuation_token = cur_comp_resp.get('continuationToken')

        if continuation_token is None:
            break

    logger.debug(f"Found {len(all_repo_items)} items in repository: '{repo_name}'")

    return all_repo_items


def download_components_from_repositories(selected_repos):
    for repo_name in selected_repos:
        all_repo_items = get_items_from_repo(repo_name)

        if not all_repo_items:
            logger.debug(f'No artifacts found in {repo_name}')
        else:
            logger.debug(f'Found {len(all_repo_items)} artifacts in {repo_name}')
            cur_dest_folder = os.path.join(config.scan_dir, repo_name)
            os.makedirs(cur_dest_folder, exist_ok=True)
            logger.info('Retrieving artifacts...')

            manager = Manager()
            docker_images_q = manager.Queue()
            with Pool(config.threads_number) as pool:
                pool.starmap(repo_worker, [(comp, repo_name, cur_dest_folder, config.headers, config, docker_images_q)
                                           for i, comp in enumerate(all_repo_items)])
            # Updating UA env vars to include Docker images from Nexus
            docker_images = set()
            while not docker_images_q.empty():
                docker_images.add(docker_images_q.get(block=True, timeout=0.05))

            if docker_images:
                config.is_docker_scan = True
                logger.info(f"Found total {len(docker_images)} docker images")
                config.docker_images = docker_images


def call_nexus_api(url: str,
                   headers: dict = None,
                   include_resp_headers: bool = False,
                   method: str = "get",
                   **kwargs) -> Union[dict, bytes, Tuple[List[dict], dict]]:
    if headers is None:
        headers = config.headers

    if not url.startswith("http"):
        url = urljoin(config.nexus_base_url, url)
    logger.debug(f"Calling Nexus URL: {url}")
    try:
        resp = requests.request(method=method, url=url, headers=headers, **kwargs)
        resp.raise_for_status()
    except requests.exceptions.RequestException:
        logging.exception(f"Received Error on endpoint: {url}")
        raise

    try:
        ret = json.loads(resp.text)
    except json.decoder.JSONDecodeError:
        logger.debug("Response is not JSON")
        ret = resp.content

    logger.debug(f"Response return type: {type(ret)}")

    if include_resp_headers:
        ret = ret, resp.headers

    return ret


def handle_docker_repo(component: dict, conf) -> str:
    """
    Locally pull Docker Image from a given repository (component)
    :param component:
    :param conf: global config
    :return: Retrieve Docker Image ID so UA will only scan images downloaded from Nexus
    """
    def get_repos_as_dict(c) -> dict:
        """
        Convert repository data into dictionary
        :returns name -> repo dictionary
        :rtype: dict
        """
        repos_list = call_nexus_api(conf.nexus_base_url + conf.resources_url, c.headers)
        logger.debug(f"found {len(repos_list)} repositories")
        repo_dict = {}
        for r in repos_list:
            repo_dict[r['name']] = r

        return repo_dict

    def get_docker_repo_url(repository: dict) -> str:
        """
        Retrieves Repository URL with port
        :param repository:
        :return: Image ID in form of string
        """
        https_port = repository['docker'].get('httpsPort')
        http_port = repository['docker']['httpPort']
        parsed_url = urlparse(repository['url'])
        if http_port:
            r_url = f"{parsed_url.hostname}:{http_port}"
        elif https_port:
            r_url = f"{parsed_url.hostname}:{https_port}"
        else:
            logger.error("Unable to get repository port. Using default URL")
            r_url = f"{parsed_url.hostname}:{parsed_url.port}"
        logger.debug(f"Returned docker repo URL: {r_url}")

        return r_url

    ret = None
    dl_url = component['assets'][0]["downloadUrl"]
    logger.debug(f"Component repository: {component['repository']}")
    logger.debug(f"Getting manifest file from: {dl_url}")
    manifest = call_nexus_api(dl_url, conf.headers)
    repos = get_repos_as_dict(conf)

    try:
        import docker
    except ImportError:
        logger.error("Found Docker repository but Docker package is not installed.")
        return ret

    repo = repos.get(component['repository'])

    ret = None

    if conf.nexus_alt_docker_registry_address:
        docker_repo_url = conf.nexus_alt_docker_registry_address
        logger.info(f"Using user-defined docker registry URL: {docker_repo_url}")
    elif repo:
        logger.debug(f"Repository data: {repo}")
        docker_repo_url = get_docker_repo_url(repo)

    if docker_repo_url:
        image_name = f"{docker_repo_url}/{manifest['name']}"
        image_full_name = f"{image_name}:{manifest['tag']}"
        logger.info(f"Pulling Docker image: {image_name}")
        try:
            docker_client = docker.from_env(timeout=DOCKER_TIMEOUT)
            # Configuring Nexus user and password are mandatory for non-anonymous Docker repositories
            docker_client.login(username=conf.nexus_user, password=conf.nexus_password, registry=docker_repo_url)
            pull_res = docker_client.images.pull(image_full_name)
            logger.debug(f"Image ID: {image_full_name} successfully pulled")
            ret = image_name
        except docker.errors.DockerException:
            logging.exception(f"Error loading image: {image_name}")
    else:
        logger.warning(f"Repository was not found for {component['repository']}. Skipping")

    return ret


def repo_worker(comp, repo_name, cur_dest_folder, headers, conf, d_images_q):
    all_components = []
    component_assets = comp['assets']
    logger.debug(f"Handling component ID: {comp['id']} on repository: {comp['repository']} Format: {comp['format']}")
    if comp['format'] == 'nuget':
        comp_name = '{}.{}.nupkg'.format(comp['name'], comp['version'])
        all_components.append(comp_name)
    elif re.match('(maven).*', comp['format']):
        component_assets_size = len(component_assets)
        for asset in range(0, component_assets_size):
            comp_name = component_assets[asset]['path'].rpartition('/')[-1]
            if comp_name.split(".")[-1] == "jar":
                all_components.append(comp_name)
    elif comp['format'] == 'docker':
        image_id = handle_docker_repo(comp, conf)
        if image_id:
            d_images_q.put(image_id)
    else:
        comp_name = component_assets[0]['path'].rpartition('/')[-1]
        all_components.append(comp_name)

    for comp_name in all_components:
        comp_worker(repo_name, component_assets, cur_dest_folder, headers, comp_name)


def comp_worker(repo_name, component_assets, cur_dest_folder, headers, comp_name):
    logger.info(f"Downloading '{comp_name}' component from: '{repo_name}'")
    comp_download_url = component_assets[0]["downloadUrl"]
    comp_data = call_nexus_api(comp_download_url, headers)
    logger.debug(f"Download URL: {comp_download_url}")
    os.makedirs(cur_dest_folder, exist_ok=True)

    with open(os.path.join(cur_dest_folder, comp_name), 'wb') as f:
        f.write(comp_data)
    logger.info(f'Component {comp_name} has successfully downloaded')


def execute_scan() -> int:
    config.ws_conn.ua_conf.productName = config.product_name
    config.ws_conn.ua_conf.checkPolicies = strtobool(config.policies)
    config.ws_conn.ua_conf.forceCheckAllDependencies = strtobool(config.policies)
    config.ws_conn.ua_conf.offline = True if os.environ.get("OFFLINE", "").lower() == "true" else False

    if config.is_docker_scan:
        config.ws_conn.ua_conf.resolveAllDependencies = True
        config.ws_conn.ua_conf.archiveExtractionDepth = 3
        config.ws_conn.ua_conf.archiveIncludes = list(ws_constants.UAArchiveFiles.ALL_ARCHIVE_FILES)
        ret = config.ws_conn.scan_docker(product_name=config.product_name, docker_images=config.docker_images)
    else:
        config.ws_conn.ua_conf.projectPerFolder = True
        ret = config.ws_conn.scan(scan_dir=config.scan_dir,
                                  product_name=config.product_name)
    logger.debug(f"Unified Agent standard output:\n {ret[1]}")
    try:
        app_status = config.ws_conn.get_last_scan_process_status(ret[2])
    except ws_sdk.ws_errors.WsSdkServerInsufficientPermissions:
        logger.debug("Insufficient permissions to execute call")

    return ret[0]


def get_repos_to_scan() -> List[str]:
    all_repos = retrieve_nexus_repositories()
    logger.debug(f"The following repositories were found: {all_repos}")
    repos_to_scan = []
    if config.nexus_repos:
        for defined_repo in config.defined_nexus_repo_l:
            if defined_repo in all_repos:
                repos_to_scan.append(defined_repo)
                logger.debug(f"Repository: '{defined_repo}' was added to scan")
            else:
                logger.error(f"User defined repository: '{defined_repo}' was not found in Nexus and will be skipped")
        if not repos_to_scan:
            logger.error("No configured repositories were found in Nexus. Nothing to scan.")
            exit(-1)
    else:
        repos_to_scan = all_repos

    if config.nexus_exc_repos:
        logger.info(f"Repositories: {config.nexus_exc_repos_l} are excluded from the scan")
        repos_to_scan = [repo for repo in repos_to_scan if repo not in config.nexus_exc_repos_l]

    logger.info(f'The following repositories will be scanned: {repos_to_scan}')

    return repos_to_scan


def read_conf_file(conf_file: str) -> ConfigParser:
    if os.path.isfile(conf_file):
        logger.debug(f"Using configuration file: '{conf_file}'")
        c = ConfigParser()
        c.optionxform = str
        c.read(conf_file)
        return c
    else:
        print("""Missing configuration file. Be sure to create params.config file with the following values:\"
[Nexus Settings]
NexusBaseUrl=
NexusAuthToken=
NexusUser=
NexusPassword=
NexusRepositories=
NexusExcludedRepositories=
NexusAltDockerRegistryAddress=


[WhiteSource Settings]
WSUserKey=
WSApiKey=
WSProductName=Nexus
WSCheckPolicies=False
WSUrl=
WSLang=

[General Settings]
ThreadCount=1
WorkDir=
JavaBin=
                    """)
        exit(-1)


def main():
    global config
    conf_file = 'params.config'
    if len(sys.argv) > 1:
        conf_file = sys.argv[1]
    params_f = read_conf_file(conf_file)

    config = Config(params_f)
    selected_repositories = get_repos_to_scan()
    config.resources_url = set_nexus_resources_url(config.nexus_version)
    download_components_from_repositories(selected_repositories)
    return_code = execute_scan()

    return return_code


if __name__ == '__main__':
    main()
