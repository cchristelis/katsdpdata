#!groovy

def katsdp = fileLoader.fromGit('scripts/katsdp.groovy', 'git@github.com:ska-sa/katsdpjenkins', 'master', 'katpull', '')

katsdp.setDependencies(['ska-sa/katsdpdockerbase/master'])
katsdp.standardBuild(maintainer: 'cschollar@ska.ac.za', subdir: 'psrchive', docker_timeout: [time: 120, unit: 'MINUTES'])
katsdp.standardBuild(maintainer: 'bmerry@ska.ac.za', subdir: 'katsdpfilewriter')
katsdp.standardBuild(maintainer: 'thomas@ska.ac.za')
