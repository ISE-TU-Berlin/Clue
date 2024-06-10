from os import path
import subprocess
import time
import kubernetes
import docker

from yaml_patch import patch_yaml


from experiment import Experiment
from experiment_environment import ExperimentEnvironment
from scaling_experiment_setting import ScalingExperimentSetting
from experiment_autoscaling import ExperimentAutoscaling


class ExperimentDeployer:

    def __init__(self, experiment: Experiment):
        self.experiment = experiment
        self.docker_client = docker.from_env()
        self.env = ExperimentEnvironment()

    def build_images(self):
        """
        build all the images for the experiment and push them to the docker registry.

        perfrom some patching of the build scripts to use buildx (for multi-arch builds)
        """

        exp = self.experiment

        git = subprocess.check_call(
            ["git", "switch", exp.target_branch], cwd=path.join(self.env.teastore_path)
        )
        if git != 0:
            raise RuntimeError(f"failed to swich git to {exp.target_branch}")

        print(f"deploying {exp.target_branch}")

        # ensure mvn build ...
        # docker run -v foo:/mnt --rm -it --workdir /mnt  maven mvn clean install -DskipTests
        mvn = self.docker_client.containers.run(
            image="maven",
            auto_remove=True,
            volumes={
                path.abspath(path.join(self.env.teastore_path)): {
                    "bind": "/mnt",
                    "mode": "rw",
                }
            },
            working_dir="/mnt",
            command="mvn clean install -DskipTests",
            # command="tail -f /dev/null",
        )
        if "BUILD SUCCESS" not in mvn.decode("utf-8"):
            raise RuntimeError(
                "failed to build teastore. Run mvn clean install -DskipTests manually and see why it fails"
            )
        else:
            print("rebuild java deps")

        # patch build_docker.sh to use buildx
        with open(
            path.join(self.env.teastore_path, "tools", "build_docker.sh"), "r"
        ) as f:
            script = f.read()

        if "buildx" in script:
            print("buildx already used")
        else:
            script = script.replace(
                "docker build",
                f"docker buildx build --platform {self.env.remote_platform_arch}",
            )
            with open(
                path.join(self.env.teastore_path, "tools", "build_docker.sh"), "w"
            ) as f:
                f.write(script)

        # 2. cd tools && ./build_docker.sh -r <env["docker_user"]/ -p && cd ..
        build = subprocess.check_call(
            ["sh", "build_docker.sh", "-r", f"{self.env.docker_user}/", "-p"],
            cwd=path.join(self.env.teastore_path, "tools"),
        )

        if build != 0:
            raise RuntimeError(
                "failed to build docker images. Run build_docker.sh manually and see why it fails"
            )

        print(f"build {self.env.docker_user}/* images")

    def deploy_branch(self, observations: str = "data/default"):
        """
        deploy the helm chart with the given values.yaml,
        patching the values.yaml before deployment:
            - replace the docker user with the given user
            - replace the tag to ensure images are pulled
            - replace the node selector to ensure we only run on nodes that we can observe (require nodes to run scaphandre)
            - apply any patches given in the experiment (see yaml_patch)

        wait for the deployment to be ready, or timeout after 3 minutes
        """

        exp = self.experiment

        with open(
            path.join(self.env.teastore_path, "examples", "helm", "values.yaml"), "r"
        ) as f:
            values = f.read()
            values = values.replace("descartesresearch", self.env.docker_user)
            # ensure we only run on nodes that we can observe
            values = values.replace(
                r"nodeSelector: {}", r'nodeSelector: {"scaphandre": "true"}'
            )
            values = values.replace("pullPolicy: IfNotPresent", "pullPolicy: Always")
            values = values.replace(r'tag: ""', r'tag: "latest"')
            if exp.autoscaling:
                values = values.replace(r"enabled: false", "enabled: true")
                # values = values.replace(r"clientside_loadbalancer: false",r"clientside_loadbalancer: true")
                if exp.autoscaling == ScalingExperimentSetting.MEMORYBOUND:
                    values = values.replace(
                        r"targetCPUUtilizationPercentage: 80",
                        r"# targetCPUUtilizationPercentage: 80",
                    )
                    values = values.replace(
                        r"# targetMemoryUtilizationPercentage: 80",
                        r"targetMemoryUtilizationPercentage: 80",
                    )
                elif exp.autoscaling == ScalingExperimentSetting.BOTH:
                    values = values.replace(
                        r"# targetMemoryUtilizationPercentage: 80",
                        r"targetMemoryUtilizationPercentage: 80",
                    )


        patch_yaml(values, exp.patches)

        with open(
            path.join(self.env.teastore_path, "examples", "helm", "values.yaml"), "w"
        ) as f:
            f.write(values)

        # write copy of used values to observations
        with open(path.join(observations, "values.yaml"), "w") as f:
            f.write(values)

        helm_deploy = subprocess.check_output(
            ["helm", "install", "teastore", "-n", exp.namespace, "."],
            cwd=path.join(self.env.teastore_path, "examples", "helm"),
        )
        helm_deploy = helm_deploy.decode("utf-8")
        if not "STATUS: deployed" in helm_deploy:
            raise RuntimeError(
                "failed to deploy helm chart. Run helm install manually and see why it fails"
            )

        self.wait_until_services_ready(
            ["teastore-auth", "teastore-registry", "teastore-webui"],
            180,
            namespace=exp.namespace,
        )

        if exp.autoscaling:
            ExperimentAutoscaling.setup_autoscaleing()

    def wait_until_services_ready(services, timeout, namespace="default"):

        v1 = kubernetes.client.AppsV1Api()
        ready_services = set()
        start_time = time.time()
        services = set(services)
        while (
            len(ready_services) < len(services) and time.time() - start_time < timeout
        ):
            for service in services.difference(
                ready_services
            ):  # only check services that are not ready yet
                try:
                    service_status = v1.read_namespaced_stateful_set_status(
                        service, namespace
                    )
                    if (
                        service_status.status.ready_replicas
                        and service_status.status.ready_replicas > 0
                    ):
                        ready_services.add(service)
                except Exception as e:
                    print(e)
                    pass
            if services == ready_services:
                return True
            time.sleep(1)
            print("waiting for deployment to be ready")
        raise RuntimeError(
            "Timeout reached. The following services are not ready: "
            + str(list(set(services) - set(ready_services)))
        )