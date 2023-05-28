import shutil
import tempfile
from collections.abc import AsyncGenerator, Callable, Generator
from pathlib import Path
from uuid import uuid4

import requests
from authlib.jose import JsonWebKey, JsonWebToken, jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError, jwt
from sqlalchemy.orm.session import Session

from mealie.core.config import get_app_dirs, get_app_settings
from mealie.db.db_setup import generate_session
from mealie.repos.all_repositories import get_repositories
from mealie.schema.user import PrivateUser, TokenData
from mealie.schema.user.user import DEFAULT_INTEGRATION_ID

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/auth/token")
oauth2_scheme_soft_fail = OAuth2PasswordBearer(tokenUrl="/api/auth/token", auto_error=False)
ALGORITHM = "HS256"
app_dirs = get_app_dirs()
settings = get_app_settings()


async def is_logged_in(token: str = Depends(oauth2_scheme_soft_fail), session=Depends(generate_session)) -> bool:
    """
    When you need to determine if the user is logged in, but don't need the user, you can use this
    function to return a boolean value to represent if the user is logged in. No Auth exceptions are raised
    if the user is not logged in. This behavior is not the same as 'get_current_user'

    Args:
        token (str, optional): [description]. Defaults to Depends(oauth2_scheme_soft_fail).
        session ([type], optional): [description]. Defaults to Depends(generate_session).

    Returns:
        bool: True = Valid User / False = Not User
    """
    try:
        payload = jwt.decode(token, settings.SECRET, algorithms=[ALGORITHM])
        user_id: str = payload.get("sub")
        long_token: str = payload.get("long_token")

        if long_token is not None:
            try:
                if validate_long_live_token(session, token, payload.get("id")):
                    return True
            except Exception:
                return False

        return user_id is not None

    except Exception:
        return False


def get_jwks():
    with requests.get(settings.OIDC_JWKS_URL) as response:
        response.raise_for_status()
        return JsonWebKey.import_key_set(response.json())


async def get_current_user(
    request: Request, token: str = Depends(oauth2_scheme), session=Depends(generate_session), jwks=Depends(get_jwks)
) -> PrivateUser:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if request.cookies["auth.strategy"] == "oidc":
        claims = JsonWebToken(["RS256"]).decode(
            s=request.cookies["auth._id_token.oidc"],
            key=jwks,
        )

        repos = get_repositories(session)
        user = repos.users.get_one(claims["email"], "email", any_case=True)

        if settings.ALLOW_OIDC_SIGNUP and user is None:
            is_admin = settings.OIDC_ADMIN_GROUP in claims["groups"]

            user = repos.users.create(
                {
                    "username": claims["preferred_username"],
                    "password": "ODIC",
                    "full_name": claims["name"],
                    "email": claims["email"],
                    "admin": is_admin,
                    "no_password_login": True,
                },
            )

        return user
    else:
        try:
            payload = jwt.decode(token, settings.SECRET, algorithms=[ALGORITHM])
            user_id: str = payload.get("sub")
            long_token: str = payload.get("long_token")

            if long_token is not None:
                return validate_long_live_token(session, token, payload.get("id"))

            if user_id is None:
                raise credentials_exception

            token_data = TokenData(user_id=user_id)
        except JWTError as e:
            raise credentials_exception from e

        repos = get_repositories(session)

        user = repos.users.get_one(token_data.user_id, "id", any_case=False)

        if user is None:
            raise credentials_exception

        token_data = TokenData(user_id=user_id)
    except JWTError as e:
        raise credentials_exception from e

    repos = get_repositories(session)

    user = repos.users.get_one(token_data.user_id, "id", any_case=False)

    # If we don't commit here, lazy-loads from user relationships will leave some table lock in postgres
    # which can cause quite a bit of pain further down the line
    session.commit()
    if user is None:
        raise credentials_exception
    return user


async def get_integration_id(request: Request, token: str = Depends(oauth2_scheme)) -> str:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if request.cookies["auth.strategy"] == "oidc":
        return DEFAULT_INTEGRATION_ID

    try:
        decoded_token = jwt.decode(token, settings.SECRET, algorithms=[ALGORITHM])
        return decoded_token.get("integration_id", DEFAULT_INTEGRATION_ID)

    except JWTError as e:
        raise credentials_exception from e


async def get_admin_user(current_user: PrivateUser = Depends(get_current_user)) -> PrivateUser:
    if not current_user.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN)
    return current_user


def validate_long_live_token(session: Session, client_token: str, user_id: str) -> PrivateUser:
    repos = get_repositories(session)

    token = repos.api_tokens.multi_query({"token": client_token, "user_id": user_id})

    try:
        return token[0].user
    except IndexError as e:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED) from e


def validate_file_token(token: str | None = None) -> Path:
    """
    Args:
        token (Optional[str], optional): _description_. Defaults to None.

    Raises:
        HTTPException: 400 Bad Request when no token or the file doesn't exist
        HTTPException: 401 Unauthorized when the token is invalid
    """
    if not token:
        raise HTTPException(status.HTTP_400_BAD_REQUEST)

    try:
        payload = jwt.decode(token, settings.SECRET, algorithms=[ALGORITHM])
        file_path = Path(payload.get("file"))
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="could not validate file token",
        ) from e

    if not file_path.exists():
        raise HTTPException(status.HTTP_400_BAD_REQUEST)

    return file_path


def validate_recipe_token(token: str | None = None) -> str:
    """
    Args:
        token (Optional[str], optional): _description_. Defaults to None.

    Raises:
        HTTPException: 400 Bad Request when no token or the recipe doesn't exist
        HTTPException: 401 JWTError when token is invalid

    Returns:
        str: token data
    """
    if not token:
        raise HTTPException(status.HTTP_400_BAD_REQUEST)

    try:
        payload = jwt.decode(token, settings.SECRET, algorithms=[ALGORITHM])
        slug: str | None = payload.get("slug")
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="could not validate file token",
        ) from e

    if slug is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST)

    return slug


async def temporary_zip_path() -> AsyncGenerator[Path, None]:
    app_dirs.TEMP_DIR.mkdir(exist_ok=True, parents=True)
    temp_path = app_dirs.TEMP_DIR.joinpath("my_zip_archive.zip")

    try:
        yield temp_path
    finally:
        temp_path.unlink(missing_ok=True)


async def temporary_dir() -> AsyncGenerator[Path, None]:
    temp_path = app_dirs.TEMP_DIR.joinpath(uuid4().hex)
    temp_path.mkdir(exist_ok=True, parents=True)

    try:
        yield temp_path
    finally:
        shutil.rmtree(temp_path)


def temporary_file(ext: str = "") -> Callable[[], Generator[tempfile._TemporaryFileWrapper, None, None]]:
    """
    Returns a temporary file with the specified extension
    """

    def func():
        temp_path = app_dirs.TEMP_DIR.joinpath(uuid4().hex + ext)
        temp_path.touch()

        with tempfile.NamedTemporaryFile(mode="w+b", suffix=ext) as f:
            try:
                yield f
            finally:
                temp_path.unlink(missing_ok=True)

    return func
