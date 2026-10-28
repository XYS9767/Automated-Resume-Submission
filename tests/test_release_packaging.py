import copy
from pathlib import Path
import zipfile
import pytest
import yaml
from build_release import PAYLOAD, APP, check_archive, validate_public_config


def template():
    return yaml.safe_load((Path(__file__).parents[1] / 'packaging/config.yaml').read_text(encoding='utf-8'))


def test_release_configuration_has_no_personal_credentials_or_resume():
    config = template()
    validate_public_config(config)
    assert config['search']['salary'] == '15-20K'
    assert config['search']['source'] == 'config'
    assert config['submit']['daily_limit'] == 150


@pytest.mark.parametrize('field', ['token', 'api_key', 'password'])
def test_release_rejects_credentials(field):
    config = copy.deepcopy(template())
    config['risk_check']['api'][field] = 'example-secret-never-publish'
    with pytest.raises(ValueError, match='credential'):
        validate_public_config(config)


def test_release_rejects_personal_resume_reference():
    config = copy.deepcopy(template())
    config['resume']['file_path'] = 'private-resume.txt'
    with pytest.raises(ValueError, match='resume'):
        validate_public_config(config)


def test_archive_allowlist_rejects_cookie_and_traversal(tmp_path):
    for extra in ('cookies.json', '../outside.txt', 'browser_data/Login Data'):
        archive = tmp_path / 'test.zip'
        with zipfile.ZipFile(archive, 'w') as z:
            for name in (*PAYLOAD, APP + '.exe', 'LICENSE', 'build-info.json'):
                data = yaml.safe_dump(template()) if name == 'config.yaml' else 'test'
                z.writestr('release/' + name, data)
            z.writestr('release/' + extra, 'must not ship')
        with pytest.raises(ValueError, match='unexpected'):
            check_archive(archive, 'release')
