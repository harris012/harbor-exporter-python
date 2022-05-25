"""
Functions related to obtaining the timestamp of images.
"""

import urllib.request
import urllib.parse
import dateutil.parser
import json
import logging
import time
from cachetools.func import ttl_cache
from collections import defaultdict
DOCKER_REGISTRY_HOST = "WILL NOT TELL YOU" # edit me
DOCKER_REGISTRY_URL = f"https://{DOCKER_REGISTRY_HOST}/v2"
IMAGE_LAST_MODIFIED_TIMESTAMP = "image_last_modified_timestamp"
IMAGE_AGE_RETRIEVAL_ERROR = "image_age_retrieval_error"
IMAGE_MISSING = "image_missing"


def iter_chunked(function, chunksize=500, **kwargs):
    pods = function(
        limit=chunksize,
        **kwargs,
    )
    yield from pods.items
    while pods.metadata._continue is not None:
        pods = function(
            limit=chunksize,
            _continue=pods.metadata._continue,
            **kwargs,
        )
        yield from pods.items


@ttl_cache(maxsize=1, ttl=3600)
def get_image_metrics(client):
    """Iterate over pods and produce image timestamp metrics.

    Parameters
    ----------
    client : k8s.client
        The client

    Returns
    -------
    collections.defaultdict
        The metrics
    """
    metrics = defaultdict(float)
    start_time = time.time()

    namespace_to_tenant_mapping = get_namespace_to_tenant_mapping(client)

    pod_iterator = iter_chunked(client.list_pod_for_all_namespaces,
                                field_selector="status.phase=Running")

    for pod in pod_iterator:
        namespace = pod.metadata.namespace
        tenant = namespace_to_tenant_mapping.get(namespace, 'system')

        for container in iter_containers(pod):
            registry, image, tag, is_image_internal = \
                get_image_details(container.image)

            last_modified_time = get_last_modified_timestamp(image, tag)

            if last_modified_time is None:
                metric_id = (IMAGE_MISSING,
                             (tenant, namespace, registry, image, tag,
                              str(is_image_internal).lower()))
                metrics[metric_id] = 1
                continue
            elif last_modified_time == 0:
                metric_id = (IMAGE_AGE_RETRIEVAL_ERROR,
                             (tenant, namespace, registry, image, tag,
                              str(is_image_internal).lower()))
                metrics[metric_id] = 1
                continue

            metric_id = (IMAGE_LAST_MODIFIED_TIMESTAMP,
                         (tenant, namespace, registry, image, tag,
                          str(is_image_internal).lower()))
            metrics[metric_id] = last_modified_time

    logging.getLogger().info(
        "Preparing image metrics took %s", time.time() - start_time)

    return metrics


@ttl_cache(ttl=900)
def get_namespace_to_tenant_mapping(client, default_team='system'):
    """Retrieve namespaces and return a mapping of
    namespace to team.

    If no team or label exists, uses a default team name.

    Parameters
    ----------
    client : k8s.client
        The client

    default_team : str
        The name to use if no team label exists.
        Defaults to 'system'

    Returns
    -------
    dict
        Dictionary with namespace to team mapping
    """
    namespaces = client.list_namespace()

    namespace_to_tenant_mapping = {}
    for namespace in namespaces.items:
        name = namespace.metadata.name
        labels = namespace.metadata.labels
        tenant = labels.get('team', default_team) if labels is not None \
            else default_team
        namespace_to_tenant_mapping[name] = tenant
    return namespace_to_tenant_mapping


@ttl_cache(ttl=3600)
def get_last_modified_timestamp(image, tag):
    """Retrieve image timestamp from registry and return timestamp
    If an HTTP error code (e.g. 404) is returned, return None.

    Parameters
    ----------
    image : str
        The image

    tag : str
        The tag

    Returns
    -------
    int
        UNIX timestamp or
        0 if the image exist but date could not be established
    """

    log = logging.getLogger()

    manifest_url = "{}/{}/manifests/{}".format(
        DOCKER_REGISTRY_URL, image, tag
    )

    req = urllib.request.Request(manifest_url, method='GET')

    try:
        response = urllib.request.urlopen(req)
    except urllib.error.HTTPError:
        return None
    else:
        data = dict()
        try:
            data = json.loads(response.read().decode("UTF-8"))
        except Exception:
            log.warning(
                "Failed to decode json from registry response: %s",
                response.read()
            )

        schema_version = data.get("schemaVersion", None)

        if schema_version == 1:
            if "history" in data:
                v1Compat = json.loads(data["history"][0]["v1Compatibility"])
                return dateutil.parser.parse(
                    v1Compat.get("created", "0")).timestamp()
            else:
                log.info(
                    "Failed to deduce image creation data for %s:%s",
                    image, tag
                )
                return 0

        elif schema_version == 2:
            media_type = data["config"]["mediaType"]

            config_url = "{}/{}/blobs/{}".format(
                DOCKER_REGISTRY_URL, image, data["config"]["digest"]
            )

            req = urllib.request.Request(
                config_url, headers={"Accept": media_type}, method='GET')
            try:
                response = urllib.request.urlopen(req)
            except urllib.error.HTTPError:
                return 0

            config = json.load(response)
            return dateutil.parser.parse(
                config.get("created", "0")).timestamp()

        else:
            log.info("Unknown schema version %s for %s:%s",
                     schema_version, image, tag)
            return 0


def iter_containers(pod):
    """
    iterate all containers of pod (include init containers) and return
    a generator
    """
    if pod.spec.init_containers:
        yield from pod.spec.init_containers

    yield from pod.spec.containers


def get_image_details(image_path):
    """Parse image path and split it into its components

    Parameters
    ----------
    image_path : str
        The full image path

    Returns
    -------
    str
        Registry name
    str
        Image name
    str
        Image tag
    bool
        Flag indicating if image is internal
    """

    registry = DOCKER_REGISTRY_HOST
    if "/" in image_path and "." in image_path.split("/")[0]:
        # registry probably a hostname, strip registry from image path
        registry, image_path = image_path.split("/", 1)

    # take care of situations like haproxy:latest
    if (len(image_path.split('/')) == 1):
        image_path = 'library/' + image_path

    if "@" in image_path:
        # digest format image@checksum:sum
        image, tag = image_path.split("@")
    elif ":" in image_path:
        image, tag = image_path.split(":")
    else:
        # take care of situations like haproxy
        image = image_path
        tag = "latest"

    is_image_internal = image_path.split('/')[0].lower() == 'internal'

    return registry, image, tag, is_image_internal
