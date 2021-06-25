import json
import os
import sys
import time

from github import Github, GithubException

import constants
import notify_chat
import utils

ballerina_bot_token = os.environ[constants.ENV_BALLERINA_BOT_TOKEN]

MODULE_BUILD_ACTION_FILE = "build_action_file"

MODULE_CREATED_PR = 'created_pr'
MODULE_TIMESTAMPED_VERSION = 'timestamped_version'

MODULE_STATUS = 'status'
MODULE_STATUS_IN_PROGRESS = 'in_progress'
MODULE_STATUS_COMPLETED = 'completed'

MODULE_CONCLUSION = 'conclusion'
MODULE_CONCLUSION_TIMED_OUT = 'timed_out'
MODULE_CONCLUSION_PR_PENDING = 'pr_build_pending'
MODULE_CONCLUSION_PR_CHECK_FAILURE = 'pr_check_failure'
MODULE_CONCLUSION_PR_NOT_MERGED = 'pr_not_merged'
MODULE_CONCLUSION_PR_MERGE_FAILURE = 'merge_failure'
MODULE_CONCLUSION_BUILD_PENDING = 'build_pending'
MODULE_CONCLUSION_BUILD_SUCCESS = 'build_success'
MODULE_CONCLUSION_BUILD_FAILURE = 'build_failure'
MODULE_CONCLUSION_BUILD_RELEASED = 'build_released'
MODULE_CONCLUSION_VERSION_CANNOT_BE_IDENTIFIED = 'version_not_identified'

COMMIT_MESSAGE = '[Automated] Update dependencies'
PULL_REQUEST_BODY_PREFIX = 'Update ballerina lang version to `'
PULL_REQUEST_TITLE = '[Automated] Update Dependencies (Ballerina Lang : '
AUTO_MERGE_PULL_REQUEST_TITLE = '[AUTO MERGE] Update Dependencies (Ballerina Lang : '

SLEEP_INTERVAL = 30  # 30s
MAX_WAIT_CYCLES = 120  # Initial timeout is 1h, changed to 80 & 100 m in level 5 & 6 respectively

retrigger_dependency_bump = sys.argv[1]
override_ballerina_version = sys.argv[2]
auto_merge_pull_requests = sys.argv[3]

event_type = 'workflow_dispatch'
if len(sys.argv) > 4:
    event_type = sys.argv[4]

github = Github(ballerina_bot_token)

extensions_file = {}
all_modules = []
current_level_modules = []
lang_version = ''
status_completed_modules = 0


def main():
    global lang_version
    global extensions_file
    global all_modules
    global current_level_modules

    try:
        extensions_file = utils.read_json_file(constants.EXTENSIONS_FILE)
    except Exception as e:
        print('[Error] Error while loading modules list ', e)
        sys.exit(1)

    print("Workflow invoked of type '" + event_type + "'")
    if event_type == 'schedule' and not extensions_file['auto_bump']:
        print("Schedule workflow invoked, exiting script as 'auto_bump' flag in modules_list.json is false.")
        return

    if override_ballerina_version != '':
        lang_version = override_ballerina_version
    else:
        lang_version = utils.get_latest_lang_version()

    bal_version = {
        'version': lang_version
    }
    try:
        utils.write_json_file(constants.LANG_VERSION_FILE, bal_version)
    except Exception as e:
        print('Failed to write to file latest_ballerina_lang_version.json', e)
        sys.exit()

    try:
        updated_file_content = open(constants.LANG_VERSION_FILE, 'r').read()
        update = utils.commit_file('ballerina-release',
                                   constants.LANG_VERSION_FILE, updated_file_content,
                                   constants.EXTENSIONS_UPDATE_BRANCH,
                                   '[Automated] Update Workflow Lang Version')[0]
        if update:
            utils.open_pr_and_merge('ballerina-release',
                                    '[Automated] Update Dependency Bump Workflow Triggered Version',
                                    'Update bumped ballerina lang version',
                                    constants.EXTENSIONS_UPDATE_BRANCH)
        else:
            print('No changes to ' + constants.LANG_VERSION_FILE + ' file')
    except GithubException as e:
        print('Error occurred while committing latest_ballerinalang_version.md', e)
        sys.exit(1)

    print('Workflow started with Ballerina Lang version : ' + lang_version)

    all_modules = extensions_file['modules']

    last_level = all_modules[-1]['level']

    print('Start dependency bump to extensions packed in ballerina-distribution')
    for i in range(last_level):
        current_level = i + 1
        current_level_modules = list(filter(lambda s: s['level'] == current_level, all_modules))

        for idx, module in enumerate(current_level_modules):
            print("[Info] Check lang dependency in module '" + module['name'] + "'")
            update_module(idx, current_level)

        if auto_merge_pull_requests.lower() == 'true':
            module_release_failure, payload = wait_for_current_level_build(current_level)
            if module_release_failure:
                print(payload)
                notify_chat.send_message(payload)
                sys.exit(1)
    print('Successfully bumped dependencies in extensions packed in ballerina-distribution')

    central_module_level = extensions_file['central_modules'][-1]['level']

    print('Start dependency bump to extensions available only in central')
    for j in range(last_level, central_module_level):
        current_level = j + 1
        current_level_modules = list(filter(lambda s: s['level'] == current_level, extensions_file['central_modules']))

        for idx, module in enumerate(current_level_modules):
            print("[Info] Check lang dependency in module '" + module['name'] + "'")
            update_module(idx, current_level)

        if auto_merge_pull_requests.lower() == 'true':
            _, _ = wait_for_current_level_build(current_level)
    print('Successfully bumped dependencies in extensions available in central')


def wait_for_current_level_build(level):
    global MAX_WAIT_CYCLES

    print("[Info] Waiting for level '" + str(level) + "' module build.")
    total_modules = len(current_level_modules)

    if level == 5:
        # In level 5 http takes around 30 min for PR build and build each
        # Changes timeout to 80 minutes
        MAX_WAIT_CYCLES = 140

    if level == 6:
        # In level 6 c2c takes around 40 min for PR build and build each
        # Changes timeout to 100 minutes
        MAX_WAIT_CYCLES = 200

    wait_cycles = 0
    global status_completed_modules
    status_completed_modules = 0

    while status_completed_modules != total_modules:
        for idx, module in enumerate(current_level_modules):
            if module[MODULE_STATUS] == MODULE_STATUS_IN_PROGRESS:
                if module[MODULE_CONCLUSION] == MODULE_CONCLUSION_PR_PENDING:
                    check_pending_pr_checks(idx)
                else:
                    # Build checks test
                    check_pending_build_checks(idx)

        if wait_cycles < MAX_WAIT_CYCLES:
            time.sleep(SLEEP_INTERVAL)
            wait_cycles = wait_cycles + 1
        else:
            # Force stop script with all in progress modules printed
            print('Dependency bump script timed out. Following modules are in pending state')
            for module in current_level_modules:
                if module[MODULE_STATUS] == MODULE_STATUS_IN_PROGRESS:
                    print(module['name'])
            sys.exit(1)

    module_release_failure = False
    payload = {
        'ballerinaVersion': lang_version,
        'prChecksFailed': [],
        'prMergeFailed': [],
        'buildCheckFailed': [],
        'buildVersionCannotBeIdentified': []
    }
    pr_checks_failed_modules = list(
        filter(lambda s: s[MODULE_CONCLUSION] == MODULE_CONCLUSION_PR_CHECK_FAILURE, current_level_modules))
    if len(pr_checks_failed_modules) != 0:
        module_release_failure = True
        print('Following modules\' Automated Dependency Update PRs have failed checks...\n')
        for module in pr_checks_failed_modules:
            payload['prChecksFailed'].append({
                'name': module['name'],
                'link': module[MODULE_CREATED_PR].html_url
            });
            print(module['name'] + " (" + module[MODULE_CREATED_PR].html_url + ")\n")

    pr_merged_failed_modules = list(
        filter(lambda s: s[MODULE_CONCLUSION] == MODULE_CONCLUSION_PR_MERGE_FAILURE, current_level_modules))
    if len(pr_merged_failed_modules) != 0:
        module_release_failure = True
        print('Following modules\' Automated Dependency Update PRs could not be merged...\n')
        for module in pr_merged_failed_modules:
            payload['prMergeFailed'].append({
                'name': module['name'],
                'link': module[MODULE_CREATED_PR].html_url
            });
            print(module['name'] + " (" + module[MODULE_CREATED_PR].html_url + ")\n")

    build_checks_failed_modules = list(
        filter(lambda s: s[MODULE_CONCLUSION] == MODULE_CONCLUSION_BUILD_FAILURE, current_level_modules))
    if len(build_checks_failed_modules) != 0:
        module_release_failure = True
        print('Following modules\' Timestamped Build checks have failed...\n')
        for module in build_checks_failed_modules:
            build_actions_page = constants.BALLERINA_ORG_URL + module['name'] + "/actions/workflows/" + \
                                 module[MODULE_BUILD_ACTION_FILE] + ".yml"
            payload['buildCheckFailed'].append({
                'name': module['name'],
                'link': build_actions_page
            });
            print(module['name'] + " (" + build_actions_page + ")\n")

    build_version_failed_modules = list(
        filter(lambda s: s[MODULE_CONCLUSION] == MODULE_CONCLUSION_VERSION_CANNOT_BE_IDENTIFIED, current_level_modules))
    if len(build_version_failed_modules) != 0:
        module_release_failure = True
        print('Following modules\' latest Timestamped Build Version cannot be identified...\n')
        for module in build_version_failed_modules:
            build_actions_page = constants.BALLERINA_ORG_URL + module['name'] + "/actions/workflows/" + \
                                 module[MODULE_BUILD_ACTION_FILE] + ".yml"
            payload['buildVersionCannotBeIdentified'].append({
                'name': module['name'],
                'link': build_actions_page
            });
            print(module['name'] + " (" + build_actions_page + ")\n")

    return module_release_failure, payload


def check_pending_pr_checks(index: int):
    module = current_level_modules[index]
    global status_completed_modules
    print("[Info] Checking the status of the dependency bump PR in module '" + module['name'] + "'")
    passing = True
    pending = False
    count = 0
    repo = github.get_repo(constants.BALLERINA_ORG_NAME + '/' + module['name'])

    failed_pr_checks = []
    pull_request = repo.get_pull(module[MODULE_CREATED_PR].number)
    sha = pull_request.head.sha
    for pr_check in repo.get_commit(sha=sha).get_check_runs():
        count += 1
        # Ignore codecov checks temporarily due to bug
        if not pr_check.name.startswith('codecov'):
            if pr_check.status != 'completed':
                pending = True
                break
            elif pr_check.conclusion == 'success':
                continue
            elif (module['name'] == 'module-ballerinax-jaeger' and
                  pr_check.conclusion == 'skipped'):
                continue
            else:
                failed_pr_check = {
                    'name': pr_check.name,
                    'html_url': pr_check.html_url
                }
                failed_pr_checks.append(failed_pr_check)
                passing = False
    if count < 1:
        # Here the checks have not been triggered yet.
        return
    if not pending:
        if passing:
            if module['auto_merge'] & ('AUTO MERGE' in pull_request.title):
                try:
                    pull_request.merge()
                    log_message = "[Info] Automated version bump PR merged for module '" + module['name'] \
                                  + "'. PR: " + pull_request.html_url
                    print(log_message)
                    current_level_modules[index][MODULE_CONCLUSION] = MODULE_CONCLUSION_BUILD_PENDING
                except Exception as e:
                    print("[Error] Error occurred while merging dependency PR for module '" +
                          current_level_modules[index]['name'] + "'", e)
                    current_level_modules[index][MODULE_STATUS] = MODULE_STATUS_COMPLETED
                    current_level_modules[index][MODULE_CONCLUSION] = MODULE_CONCLUSION_PR_MERGE_FAILURE
                    status_completed_modules += 1
            else:
                current_level_modules[index][MODULE_STATUS] = MODULE_STATUS_COMPLETED
                current_level_modules[index][MODULE_CONCLUSION] = MODULE_CONCLUSION_PR_NOT_MERGED
                status_completed_modules += 1

        else:
            current_level_modules[index][MODULE_STATUS] = MODULE_STATUS_COMPLETED
            current_level_modules[index][MODULE_CONCLUSION] = MODULE_CONCLUSION_PR_CHECK_FAILURE
            module_name = module['name']
            print("[Error] Dependency bump PR checks have failed for '" + module_name + "'")
            for check in failed_pr_checks:
                print("[" + module_name + "] PR check '" + check["name"] + "' failed for " + check["html_url"])
            status_completed_modules += 1


def check_pending_build_checks(index: int):
    module = current_level_modules[index]
    global status_completed_modules
    print("[Info] Checking the status of the timestamped build in module '" + module['name'] + "'")
    passing = True
    pending = False
    build_check_found = False  # This is to stop intermittent failures
    repo = github.get_repo(constants.BALLERINA_ORG_NAME + '/' + module['name'])
    pull_request = repo.get_pull(module[MODULE_CREATED_PR].number)
    sha = pull_request.merge_commit_sha

    failed_build_name, failed_build_html = [], []
    if module[MODULE_CONCLUSION] == MODULE_CONCLUSION_BUILD_PENDING:
        for build_check in repo.get_commit(sha=sha).get_check_runs():
            build_check_found = True
            # Ignore codecov checks temporarily due to bug
            if not build_check.name.startswith('codecov'):
                if build_check.status != 'completed':
                    pending = True
                    break
                elif build_check.conclusion == 'success':
                    continue
                else:
                    failed_build_name.append(build_check.name)
                    failed_build_html.append(build_check.html_url)
                    passing = False
        if build_check_found and not pending:
            if passing:
                current_level_modules[index][MODULE_CONCLUSION] = MODULE_CONCLUSION_BUILD_SUCCESS
            else:
                current_level_modules[index][MODULE_STATUS] = MODULE_STATUS_COMPLETED
                current_level_modules[index][MODULE_CONCLUSION] = MODULE_CONCLUSION_BUILD_FAILURE
                module_name = module['name']
                print("[Error] Dependency bump PR merge build checks have failed for '" + module_name + "'")
                for name, html_url in zip(failed_build_name, failed_build_html):
                    print("[" + module_name + "] Build check '" + name + "' failed for " + html_url)
                status_completed_modules += 1
    else:
        # Already successful and merged
        current_level_modules[index][MODULE_CONCLUSION] = MODULE_CONCLUSION_BUILD_SUCCESS

    if current_level_modules[index][MODULE_CONCLUSION] == MODULE_CONCLUSION_BUILD_SUCCESS:
        if current_level_modules[index]['name'] == 'ballerina-distribution':
            current_level_modules[index][MODULE_CONCLUSION] = MODULE_CONCLUSION_BUILD_RELEASED
        else:
            try:
                packages_url = 'https://api.github.com/orgs/' + constants.BALLERINA_ORG_NAME + '/packages/maven/' \
                               + module['group_id'] + '.' + module['artifact_id'] + '/versions'
                packages_list_string = utils.open_url(packages_url).read()
                packages_list = json.loads(packages_list_string)
                latest_package = packages_list[0]['name']

                if retrigger_dependency_bump.lower() == 'true':
                    for package in packages_list:
                        sha_of_released_package = package['name'].split('-')[-1]
                        if sha_of_released_package in sha:
                            latest_package = package['name']
                            break

                current_level_modules[index][MODULE_CONCLUSION] = MODULE_CONCLUSION_BUILD_RELEASED
                current_level_modules[index][MODULE_TIMESTAMPED_VERSION] = latest_package
            except Exception as e:
                print("[Error] Failed to get latest timestamped version for module '" + module['name'] + "'", e)
                current_level_modules[index][MODULE_STATUS] = MODULE_CONCLUSION_VERSION_CANNOT_BE_IDENTIFIED
        current_level_modules[index][MODULE_STATUS] = MODULE_STATUS_COMPLETED
        status_completed_modules += 1


def update_module(idx: int, current_level):
    module = current_level_modules[idx]
    repo = github.get_repo(constants.BALLERINA_ORG_NAME + '/' + module['name'])
    properties_file = repo.get_contents(constants.GRADLE_PROPERTIES_FILE)

    properties_file = properties_file.decoded_content.decode(constants.ENCODING)
    updated_properties_file = get_updated_properties_file(module['name'], current_level, properties_file)

    update = utils.commit_file(module['name'], constants.GRADLE_PROPERTIES_FILE, updated_properties_file,
                               constants.DEPENDENCY_UPDATE_BRANCH, COMMIT_MESSAGE)[0]

    if update:
        print("[Info] Update lang dependency in module '" + module['name'] + "'")
        create_pull_request(idx, repo)
    else:
        current_level_modules[idx][MODULE_STATUS] = MODULE_STATUS_IN_PROGRESS
        current_level_modules[idx][MODULE_CONCLUSION] = MODULE_CONCLUSION_BUILD_SUCCESS
        current_level_modules[idx][MODULE_CREATED_PR] = None

        pulls = repo.get_pulls(state='closed')
        sha_of_lang = lang_version.split('-')[-1]

        for pull in pulls:
            if sha_of_lang in pull.title:
                current_level_modules[idx][MODULE_CREATED_PR] = pull
                break


def get_updated_properties_file(module_name, current_level, properties_file):
    updated_properties_file = ''

    split_lang_version = lang_version.split('-')
    processed_lang_version = split_lang_version[2] + split_lang_version[3]

    for line in properties_file.splitlines():
        if line.startswith(constants.LANG_VERSION_KEY):
            current_version = line.split('=')[-1]

            split_current_version = current_version.split('-')

            if len(split_current_version) > 3:
                processed_current_version = split_current_version[2] + split_current_version[3]

                if processed_current_version < processed_lang_version:
                    print("[Info] Updating the lang version in module: '" + module_name + "'")
                    updated_properties_file += constants.LANG_VERSION_KEY + '=' + lang_version + '\n'
                else:
                    updated_properties_file += line + '\n'
            else:
                # Stable dependency & SNAPSHOT
                print("[Info] Updating the lang version in module: '" + module_name + "'")
                updated_properties_file += constants.LANG_VERSION_KEY + '=' + lang_version + '\n'
        else:
            key_found = False
            possible_dependency_modules = list(filter(lambda s: s['level'] < current_level, all_modules))

            for possible_dependency in possible_dependency_modules:
                if line.startswith(possible_dependency['version_key']):
                    updated_line = possible_dependency['version_key'] + '=' \
                                   + possible_dependency[MODULE_TIMESTAMPED_VERSION]
                    updated_properties_file += updated_line + '\n'
                    key_found = True
                    break
            if not key_found:
                updated_properties_file += line + '\n'

    return updated_properties_file


def create_pull_request(idx: int, repo):
    module = current_level_modules[idx]
    pulls = repo.get_pulls(state='open')
    pr_exists = False
    created_pr = ''

    sha_of_lang = lang_version.split('-')[-1]

    for pull in pulls:
        if pull.head.ref == constants.DEPENDENCY_UPDATE_BRANCH:
            pr_exists = True
            created_pr = pull
            pull.edit(
                title=pull.title.rsplit('-', 1)[0] + '-' + sha_of_lang + ')',
                body=pull.body.rsplit('-', 1)[0] + '-' + sha_of_lang + '` and relevant extensions.'
            )
            print("[Info] Automated version bump PR found for module '" + module['name'] + "'. PR: " + pull.html_url)
            break

    if not pr_exists:
        try:
            pull_request_title = PULL_REQUEST_TITLE
            if (auto_merge_pull_requests.lower() == 'true') & module['auto_merge']:
                pull_request_title = AUTO_MERGE_PULL_REQUEST_TITLE
            pull_request_title = pull_request_title + lang_version + ')'

            created_pr = repo.create_pull(
                title=pull_request_title,
                body=PULL_REQUEST_BODY_PREFIX + lang_version + '` and relevant extensions.',
                head=constants.DEPENDENCY_UPDATE_BRANCH,
                base=repo.default_branch
            )
            log_message = "[Info] Automated version bump PR created for module '" + module['name'] \
                          + "'. PR: " + created_pr.html_url
            print(log_message)
        except Exception as e:
            print("[Error] Error occurred while creating pull request for module '" + module['name'] + "'.", e)
            sys.exit(1)

        try:
            utils.approve_pr(module, auto_merge_pull_requests, created_pr.number)
        except Exception as e:
            print("[Error] Error occurred while approving dependency PR for module '" + module['name'] + "'", e)

    current_level_modules[idx][MODULE_CREATED_PR] = created_pr
    current_level_modules[idx][MODULE_STATUS] = MODULE_STATUS_IN_PROGRESS
    current_level_modules[idx][MODULE_CONCLUSION] = MODULE_CONCLUSION_PR_PENDING


main()
