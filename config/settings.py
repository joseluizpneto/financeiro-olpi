import os
import secrets
from pathlib import Path

import dj_database_url
from django.contrib.messages import constants as message_constants

BASE_DIR = Path(__file__).resolve().parent.parent


def env_bool(nome: str, padrao: bool = False) -> bool:
    return os.environ.get(nome, str(padrao)).strip().lower() in ("1", "true", "yes", "sim", "on")


def env_lista(nome: str, padrao: str = "") -> list[str]:
    return [x.strip() for x in os.environ.get(nome, padrao).split(",") if x.strip()]


DEBUG = env_bool("DEBUG", False)
_SECRET_ENV = os.environ.get("SECRET_KEY", "").strip()
if not DEBUG:
    if (
        not _SECRET_ENV
        or "troque" in _SECRET_ENV.lower()
        or "dev-inseguro" in _SECRET_ENV.lower()
        or "dev-local" in _SECRET_ENV.lower()
        or len(_SECRET_ENV) < 50
    ):
        raise RuntimeError(
            "Defina uma SECRET_KEY aleatória com pelo menos 50 caracteres para rodar com DEBUG=False."
        )
SECRET_KEY = _SECRET_ENV or secrets.token_urlsafe(64)

ALLOWED_HOSTS = env_lista("ALLOWED_HOSTS", "localhost,127.0.0.1")
CSRF_TRUSTED_ORIGINS = env_lista("CSRF_TRUSTED_ORIGINS")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "financeiro",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "financeiro.context_processors.contexto_global",
                "financeiro.context_processors.configuracoes_globais",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

if os.environ.get("DATABASE_URL"):
    DATABASES = {
        "default": dj_database_url.config(conn_max_age=600, conn_health_checks=True),
    }
elif os.environ.get("POSTGRES_HOST"):
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.postgresql",
            "NAME": os.environ.get("POSTGRES_DB", "financas"),
            "USER": os.environ.get("POSTGRES_USER", "financas"),
            "PASSWORD": os.environ.get("POSTGRES_PASSWORD", ""),
            "HOST": os.environ.get("POSTGRES_HOST", "db"),
            "PORT": os.environ.get("POSTGRES_PORT", "5432"),
            "CONN_MAX_AGE": 600,
            "CONN_HEALTH_CHECKS": True,
        }
    }
else:
    _sqlite_env = os.environ.get("SQLITE_PATH", "").strip()
    if _sqlite_env:
        _sqlite_path = Path(_sqlite_env)
    elif (BASE_DIR / "db.sqlite3").exists():
        _sqlite_path = BASE_DIR / "db.sqlite3"
    else:
        _sqlite_path = BASE_DIR / "data" / "db.sqlite3"
    _sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    DATABASES = {
        "default": {
            "ENGINE": "django.db.backends.sqlite3",
            "NAME": _sqlite_path,
            "CONN_MAX_AGE": 600,
            "CONN_HEALTH_CHECKS": True,
        }
    }

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 8}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "pt-br"
TIME_ZONE = os.environ.get("TZ", "America/Araguaina")
USE_I18N = True
USE_TZ = True
USE_THOUSAND_SEPARATOR = False

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"
GIT_UPDATE_DIR = Path(os.environ.get("GIT_UPDATE_DIR", str(BASE_DIR / "data" / "atualizacao")))

STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

CACHES = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

LOGIN_MAX_TENTATIVAS = int(os.environ.get("LOGIN_MAX_TENTATIVAS", "5"))
LOGIN_BLOQUEIO_MINUTOS = int(os.environ.get("LOGIN_BLOQUEIO_MINUTOS", "15"))

SESSION_COOKIE_AGE = int(os.environ.get("SESSION_COOKIE_AGE", str(60 * 60 * 24 * 14)))
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True

USAR_HTTPS = env_bool("COOKIE_SECURE", False)
if USAR_HTTPS:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
    SECURE_REDIRECT_EXEMPT = [r"^health/$"]
    SECURE_REFERRER_POLICY = "same-origin"
else:
    SESSION_COOKIE_SECURE = False
    CSRF_COOKIE_SECURE = False
    SECURE_REFERRER_POLICY = "same-origin"

MESSAGE_TAGS = {message_constants.ERROR: "danger"}

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": os.environ.get("LOG_LEVEL", "INFO")},
}
