variable "IMAGE_SOURCE" {
  default = "https://github.com/w59227c/dingtalk-travel-reimbursement"
  type    = string
}

variable "IMAGE_REVISION" {
  default = "local"
  type    = string
}

variable "BACKEND_REPOSITORY" {
  default = "expense-backend"
  type    = string
}

variable "WEB_REPOSITORY" {
  default = "expense-web"
  type    = string
}

variable "PUBLISH_TAGS" {
  default = ["unpublished"]
  type    = list(string)
}

group "ci" {
  targets = ["backend-ci", "web-ci"]
}

group "publish" {
  targets = ["backend-publish", "web-publish"]
}

target "_common" {
  platforms = ["linux/amd64"]
  labels = {
    "org.opencontainers.image.source"   = IMAGE_SOURCE
    "org.opencontainers.image.revision" = IMAGE_REVISION
  }
}

target "_backend" {
  inherits   = ["_common"]
  context    = "./backend"
  dockerfile = "Dockerfile"
  cache-from = ["type=gha,scope=backend-amd64"]
}

target "backend-ci" {
  inherits = ["_backend"]
  tags     = ["expense-backend:ci"]
  cache-to = ["type=gha,mode=max,scope=backend-amd64"]
}

target "backend-publish" {
  inherits = ["_backend"]
  tags     = [for tag in PUBLISH_TAGS : "${BACKEND_REPOSITORY}:${tag}"]
}

target "_web" {
  inherits   = ["_common"]
  context    = "."
  dockerfile = "frontend/Dockerfile"
  cache-from = ["type=gha,scope=web-amd64"]
}

target "web-ci" {
  inherits = ["_web"]
  tags     = ["expense-web:ci"]
  cache-to = ["type=gha,mode=max,scope=web-amd64"]
}

target "web-publish" {
  inherits = ["_web"]
  tags     = [for tag in PUBLISH_TAGS : "${WEB_REPOSITORY}:${tag}"]
}
