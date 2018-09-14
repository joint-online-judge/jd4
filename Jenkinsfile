pipeline {
  agent any
  stages {
    stage('Build') {
      steps {
        sh 'docker build -t jioj/jd4:latest .'
      }
    }
  }
}