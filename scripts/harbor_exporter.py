# !/usr/bin/env python
"""
LP Harbor Metrics server
This script collects data from Harbor server (https://github.com/goharbor/harbor)
Parses the data and upload it to Prometheus.
"""
import requests
import json
from prometheus_client import start_http_server,Gauge
import sys
import time
import random
import subprocess
from datetime import datetime
repos_dict = dict()


# add datetime.now to each print
old_out = sys.stdout


class stampedStdout:
    """Stamped stdout."""

    nl = True

    def write(self, x):
        """Write function overloaded."""
        if x == '\n':
            old_out.write(x)
            self.nl = True
        elif self.nl:
            old_out.write('[%s] %s' % (str(datetime.now()), str(x)))
            self.nl = False
        else:
            old_out.write(x)

    def flush(self):
        pass


sys.stdout = stampedStdout()


def check_connection():
    """ Checking connection to harbor
    Args : None
    Return : True if connection was established successfully, False otherwise.
    """
    try:
        harborurl = get_harbor_url_kubernetes()
        url = "https://{0}/api/v2.0/replication/policies".format(harborurl)
        response = requests.get(url, auth=requests.auth.HTTPBasicAuth(get_harbor_username_kubernetes(), get_harbor_password_kubernetes()))
        print(response.status_code)
        if response.status_code != 200:
            print('Connection to Harbor failed')
            return False
    except requests.exceptions.RequestException:
        print('Connection to Harbor failed')
        return False
    print("Connection to Harbor successfully established.")
    return True


def hold_while_connection_failed(sleeptime):
    """ Sleep after connection to harbor failed.
    Args : Sleep time
    Return : None.
    """
    while check_connection() == False:
        print('Going to try again in {0} seconds'.format(sleeptime))
        time.sleep(sleeptime)
        sleeptime += random.randint(10, 30)


def get_harbor_password_kubernetes():
    """ Looking for harbor password from environments.
    Args : None
    Return : harbor password.
    """
    p = subprocess.Popen('set | grep -i  HARBOR_PWD', shell=True, stdout=subprocess.PIPE)
    harbor_password, err = p.communicate()
    harbor_password = harbor_password.decode ('utf-8')
    harbor_password = harbor_password.split('\n')[1].split('=')[1]
    return harbor_password


def get_harbor_url_kubernetes():
    """ Looking for harbor url from environments.
    Args : None
    Return : harbor password.
    """
    p = subprocess.Popen('set | grep -i  HARBOR_SERVER_k8s', shell=True, stdout=subprocess.PIPE)
    harbor_url, err = p.communicate()
    harbor_url = harbor_url.decode('utf-8')
    harbor_url = harbor_url.split('\n')[1].split('=')[1]
    return harbor_url


def get_harbor_username_kubernetes():
    """ Looking for harbor username from environments.
    Args : None
    Return : harbor username.
    """
    p = subprocess.Popen('set | grep -i  HARBOR_USERNAME', shell=True, stdout=subprocess.PIPE)
    harbor_username, err = p.communicate()
    harbor_username = harbor_username.decode('utf-8')
    harbor_username = harbor_username.split('\n')[1].split('=')[1]
    return harbor_username


def get_response(url):
    try:
        harborurl = get_harbor_url_kubernetes()
        url = "https://{0}/".format(harborurl) + url
        response = requests.get(url, auth=requests.auth.HTTPBasicAuth(get_harbor_username_kubernetes(), get_harbor_password_kubernetes()))
        #print(url, response)
        if response.status_code != 200:
            print(response.text)
            return False
    except requests.exceptions.RequestException as e:
        print(e)
        print('Error: Unable to connect with api')
        return False
    return json.loads(response.text)


def prometheus_register():
    """ Register the metrics for Prometheus.
    Args : None
    Return : None
    """
    global harbor_readonly_data
    global harbor_replication_data
    global harbor_repository_data
    global harbor_repository_image_size
    global harbor_robotaccount_expiration
    harbor_replication_data = Gauge('harbor_replication_data', 'Harbor Replication Data Metrics', ['replication_id', 'replication_name', 'replication_project', 'replication_target_endpoint', 'replication_trigger_type', 'job_status'])
    harbor_repository_data = Gauge('harbor_repository_pull_count', 'Harbor Project Repository Pull Metric', ['project_id', 'project_name', 'repository_name'])
    harbor_repository_image_size = Gauge('harbor_repository_image_size', 'Harbor Repository Image Size in bytes', ['project_id', 'project_name', 'repository_name', 'image_tag'])
    harbor_robotaccount_expiration = Gauge('harbor_robotaccount_expiration', 'Harbor Project Robot Account Exiration Date', ['project_id', 'project_name', 'username'])
    harbor_readonly_data = Gauge('harbor_readonly_mode', 'Harbor Read only mode', ['harbor_server'])


def json_replication_info():
    """ creating info json with all details.
    Args : None
    Return : Info from harbor as json
    """
    url = "api/v2.0/replication/policies"
    return get_response(url)


def get_job_ids(json):
    """ scrap data from json
    Args : json_all_info func json
    Return : list with scrape info
    """
    final_list = []
    for replication_set in json:
        temp_list = []
        temp_list.append(replication_set['id'])
        temp_list.append(replication_set['name'])
        temp_list.append(replication_set['description'])
        temp_list.append(replication_set.get('dest_registry').get('name'))
        temp_list.append(replication_set.get('trigger').get('type'))
        final_list.append(temp_list)
    return final_list


# Get job status for each job in the dictionary created in function get_job_ids.
def get_job_status(replication_id_list):
    """ Collect succeed, failed, running and stopped info about each job
    Args : list contains all replication ids
    Return : None
    """
    for replication_set in replication_id_list:
        succeed_counter = 0
        failed_counter = 0
        inprogress_counter = 0
        stopped_counter = 0
        url = "api/v2.0/replication/executions?policy_id={0}".format(replication_set[0])
        json_response = get_response(url)
        for i in json_response:
            succeed_counter = succeed_counter + i['succeed']
            failed_counter = failed_counter + i['failed']
            inprogress_counter = inprogress_counter + i['in_progress']
            stopped_counter = stopped_counter + i['stopped']
        print('Gauging replication data for {0}'.format(replication_set[1]))
        # LABELS: ['replication_id', 'replication_name', 'replication_project', 'replication_target_endpoint', 'replication_trigger_type', 'job_status']
        harbor_replication_data.labels(replication_set[0], replication_set[1], replication_set[2], replication_set[3],
                                       replication_set[4], "succeed").set(succeed_counter)
        harbor_replication_data.labels(replication_set[0], replication_set[1], replication_set[2], replication_set[3],
                                       replication_set[4], "failed").set(failed_counter)
        harbor_replication_data.labels(replication_set[0], replication_set[1], replication_set[2], replication_set[3],
                                       replication_set[4], "running").set(inprogress_counter)
        harbor_replication_data.labels(replication_set[0], replication_set[1], replication_set[2], replication_set[3],
                                       replication_set[4], "stopped").set(stopped_counter)

def get_pull_status_per_project(projects_json):
    """ Collect pull_count for each repository
    Args : list contains all projects ids
    Return : None
    """
    repos_names_last_run = dict()
    for project in projects_json:
        """Iterate each project and check if repositories is less than 500
        If Project have more than 500 repos then Gauge will set repository name as Unknown so it can be
        identify later in Prometheus along with the repos count"""
        if project['repo_count'] < 1000:
            """Creating List for each Gauge type
            Project_list used for repository pull count
            Image_list used for image size"""
            project_list = []
            image_list = []
            project_list.extend([project['project_id'], project['name']])
            image_list.extend([project['project_id'], project['name']])
            """This API call is very heavy and can cause high CPU load to Harbor
            Sleeping 20 seconds between continue the code.
            API return all Repos of Project"""
            project_pull_info = get_response("api/v2.0/projects/{0}/repositories?page_size=0".format(project["name"]))
            time.sleep(20)
            if type(project_pull_info) != bool:
                for name in project_pull_info:
                    image_list.append(name['name'])
                    """repos_dict is Dictionary that contains tags_count and repos tags for each repository"""
                    """checking if repo is already in dictionary
                    If Repo does not exist, will add it and run image collect API"""
                    global repos_dict
                    repos_names_last_run[name['name']] = project['project_id']
                    if name['name'] in repos_dict:
                        """if repository tags count changed, collecting all images again
                        and finding diff of images that changed in order to remove this image tag from Prometheus"""
                        if repos_dict.get(name['name'])[0] != name['artifact_count']:
                            old_images = repos_dict[name['name']][2:]
                            print("{0} tags count change from {1} to {2}, Getting all images sizes for repository".format(name['name'], repos_dict.get(name['name'])[0], name['artifact_count']))
                            repos_dict[name['name']] = [name['artifact_count'], project['project_id']]
                            image_list = image_info(image_list, name['name'])
                            image_list = image_list[:-1]
                            repos_dict[name['name']][0] = name['artifact_count']
                            #removed_images = set(repos_dict[name['name']][2:]).difference(set(old_images))
                            removed_images = (diff(old_images,repos_dict[name['name']][2:]))
                            if len(removed_images) > 0:
                                for tag in removed_images:
                                    print("Going to set -1 to tag:{0} in project {1} under project id:{2}".format(tag,name['name'],project['project_id']))
                                    harbor_repository_image_size.labels(project['project_id'], project['name'], name['name'], tag ).set(-1)
                        else:
                            #print("{0} got the same tags_count, skipping image size gauge".format(name['name']))
                            image_list = image_list[:-1]
                    else:
                        if 'artifact_count' not in name:
                            name['artifact_count'] = 0
                        repos_dict[name['name']] = [name['artifact_count'], project['project_id']]
                        image_list = image_info(image_list, name['name'])
                        image_list = image_list[:-1]
                    project_list.extend([name['name'].split("/")[1], name.get('pull_count',0)])
                    # LABELS: ['project_id', 'project_name', 'repository_name']
                    print("Gauging pull count for repo {0}/{1}: {2}".format(project_list[1],project_list[2],project_list[3]))
                    harbor_repository_data.labels(project_list[0], project_list[1], project_list[2]).set(project_list[3])
                    project_list = project_list[:-2]
        else:
            print("ignoring project {0}: too much repositories(600 allowed".format(project['name']))
            harbor_repository_data.labels(project['project_id'], project['name'], "unknown").set(project['repo_count'])
    removed_reops_set_image_size(repos_names_last_run)


def removed_reops_set_image_size(repos_names_last_run):
    global repos_dict
    """Checking if repository was deleted during the last run
    When Repository was deleted, all tags will be gauged as size -1
    and repo will be deleted from repos_dict memory"""
    removed_repos = []
    for repo in repos_dict.keys():
        if repo not in repos_names_last_run.keys():
            for tag in repos_dict[repo][2:]:
                print("{0} Repo deleted, Gauging {1} images to size -1".format(repo, tag))
                harbor_repository_image_size.labels(repos_dict[repo][1], repo.split("/")[0], repo, tag).set(-1)
                removed_repos.append(repo)
    for repo in removed_repos:
        repos_dict.pop(repo, None)


def get_projects():
    """ Collect all projects data
    Args : None
    Return : All projects data in JSON format
    """
    url = "api/v2.0/projects"
    return get_response(url)


def image_info(image_list, name):
    global repos_dict
    repo_list = image_list[2].split("/")
    url = "api/v2.0/projects/{0}/repositories/{1}/artifacts?page_size=0".format(repo_list[0], repo_list[1])
    taginfo = get_response(url)
    print("Gauging images size for repo {0}".format(name))
    for tag in taginfo:
        repos_dict[name].append(tag['tags'][0]['name'])
        image_list.append(tag['tags'][0]['name'])
        image_list.append(tag['size'])
        harbor_repository_image_size.labels(image_list[0], image_list[1], image_list[2], image_list[3]).set(image_list[4])
        image_list = image_list[:-2]
    return image_list


def diff(A, B):
    return (list(set(A) - set(B)))


def robotaccount_expiration(projects):
    for project in projects:
        url = "api/v2.0/projects/{0}/robots".format(project['project_id'])
        robotaccounts_per_project = get_response(url)
        if len(robotaccounts_per_project) > 0:
            for robotaccount in robotaccounts_per_project:
                harbor_robotaccount_expiration.labels(project['project_id'], project['name'], robotaccount['name']).set(robotaccount['expires_at'])


def harbor_readonly():
    readonly = get_response('api/v2.0/systeminfo?page_size=0')['read_only']
    print("Harbor is on read only mode: {0}".format(readonly))
    harbor_readonly_data.labels(get_harbor_url_kubernetes()).set(readonly)


def main():
    try:
        print('Starting Prometheus web-server')
        start_http_server(14040)
        print('Registering metrics to Prometheus')
        prometheus_register()
        while True:
            if check_connection():
                harbor_readonly()
                projects = get_projects()
                get_job_status(get_job_ids(json_replication_info()))
                get_pull_status_per_project(projects)
                robotaccount_expiration(projects)
                print('Gauge finished, Sleeping 5 minutes before running again')
                time.sleep(300)
            else:
                timetosleep = 25
                hold_while_connection_failed(timetosleep)
    except KeyboardInterrupt:
        print('killed! :/')
        sys.exit(1)


if __name__ == '__main__':
    main()
