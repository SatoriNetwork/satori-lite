@echo off
setlocal enabledelayedexpansion

:: Check if Docker is installed
where docker >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: Docker is not installed. Please install Docker first.
    echo Visit: https://docs.docker.com/get-docker/
    exit /b 1
)

:: Check if Docker is running
docker info >nul 2>&1
if %errorlevel% neq 0 (
    echo Error: Docker is not running. Please start Docker and try again.
    exit /b 1
)

:: Production mode - uses Docker image code
set LOCAL_MODE=false
set IMAGE_TAG=latest
set IMAGE=satorinet/satori-lite:%IMAGE_TAG%
set BASE_PORT=24601

if not defined SATORI_RELAY_PUBLIC_BASE_PORT set SATORI_RELAY_PUBLIC_BASE_PORT=7777
if not defined SATORI_RELAY_PRIVATE_BASE_PORT set SATORI_RELAY_PRIVATE_BASE_PORT=7171

set RELAY_DOCKER_MOUNT_ARGS=
set RELAY_DOCKER_ENV_ARGS=

:: Parse "local" flag first
set "ARG1=%~1"
if /i "%ARG1%"=="local" (
    set LOCAL_MODE=true
    shift
    set "ARG1=%~1"
)

:: Parse instance number (default: 1)
set INSTANCE=1
set CMD=
if defined ARG1 (
    echo %ARG1%| findstr /r "^[0-9][0-9]*$" >nul 2>&1
    if !errorlevel! equ 0 (
        set INSTANCE=%ARG1%
        set "CMD=%~2"
    ) else (
        set "CMD=%ARG1%"
    )
)

set CONTAINER_NAME=satori-%INSTANCE%
set /a PORT=%BASE_PORT% + %INSTANCE% - 1

if not defined SATORI_RELAY_PUBLIC_PORT set /a SATORI_RELAY_PUBLIC_PORT=%SATORI_RELAY_PUBLIC_BASE_PORT% + %INSTANCE% - 1
if not defined SATORI_RELAY_PRIVATE_PORT set /a SATORI_RELAY_PRIVATE_PORT=%SATORI_RELAY_PRIVATE_BASE_PORT% + %INSTANCE% - 1

set PUBLIC_RELAY_PORT=%SATORI_RELAY_PUBLIC_PORT%
set PRIVATE_RELAY_PORT=%SATORI_RELAY_PRIVATE_PORT%
set DATA_DIR=%cd%\%INSTANCE%

:: Detect Docker socket access for relay sidecars
call :detect_relay_docker_access

:: Help
if "%CMD%"=="--help" goto :help
if "%CMD%"=="-h" goto :help

:: Commands
if "%CMD%"=="start" goto :start
if "%CMD%"=="stop" goto :stop
if "%CMD%"=="restart" goto :restart
if "%CMD%"=="logs" goto :logs
if "%CMD%"=="status" goto :status
if "%CMD%"=="" goto :cli
goto :unknown

:detect_relay_docker_access
    if defined SATORI_DOCKER_SOCKET (
        set RELAY_DOCKER_MOUNT_ARGS=-v %SATORI_DOCKER_SOCKET%:/var/run/docker.sock
        set RELAY_DOCKER_ENV_ARGS=-e DOCKER_HOST=unix:///var/run/docker.sock
        exit /b 0
    )
    if defined DOCKER_HOST (
        set RELAY_DOCKER_ENV_ARGS=-e DOCKER_HOST=%DOCKER_HOST%
        exit /b 0
    )
    :: Try default Docker Desktop named pipe
    if exist "\\.\pipe\docker_engine" (
        set RELAY_DOCKER_MOUNT_ARGS=-v //./pipe/docker_engine://./pipe/docker_engine
        set RELAY_DOCKER_ENV_ARGS=-e DOCKER_HOST=npipe:////./pipe/docker_engine
        exit /b 0
    )
    set RELAY_DOCKER_MOUNT_ARGS=
    set RELAY_DOCKER_ENV_ARGS=
    exit /b 1

:create_data_dirs
    if not exist "%DATA_DIR%\config" mkdir "%DATA_DIR%\config"
    if not exist "%DATA_DIR%\wallet" mkdir "%DATA_DIR%\wallet"
    if not exist "%DATA_DIR%\models" mkdir "%DATA_DIR%\models"
    if not exist "%DATA_DIR%\data" mkdir "%DATA_DIR%\data"
    exit /b 0

:create_container
    call :create_data_dirs

    if "%LOCAL_MODE%"=="false" (
        echo Pulling latest image...
        docker pull %IMAGE%
        set SATORI_ENV=prod
    ) else (
        echo Using local image: %IMAGE%
        set SATORI_ENV=dev
    )

    if defined SATORI_FORCE_ENV set SATORI_ENV=%SATORI_FORCE_ENV%

    set CENTRAL_URL_ARG=
    if defined SATORI_CENTRAL_URL set CENTRAL_URL_ARG=-e SATORI_CENTRAL_URL=%SATORI_CENTRAL_URL%

    set API_URL_ARG=
    if defined SATORI_API_URL set API_URL_ARG=-e SATORI_API_URL=%SATORI_API_URL%

    docker run -d --name %CONTAINER_NAME% ^
        --restart unless-stopped ^
        -p %PORT%:%PORT% ^
        -p %PUBLIC_RELAY_PORT%:%PUBLIC_RELAY_PORT% ^
        -p %PRIVATE_RELAY_PORT%:%PRIVATE_RELAY_PORT% ^
        %RELAY_DOCKER_MOUNT_ARGS% ^
        -e SATORI_ENV=%SATORI_ENV% ^
        -e SATORI_UI_PORT=%PORT% ^
        -e SATORI_RELAY_PUBLIC_PORT=%PUBLIC_RELAY_PORT% ^
        -e SATORI_RELAY_PRIVATE_PORT=%PRIVATE_RELAY_PORT% ^
        %RELAY_DOCKER_ENV_ARGS% ^
        %CENTRAL_URL_ARG% ^
        %API_URL_ARG% ^
        -v "%DATA_DIR%\config:/Satori/Neuron/config" ^
        -v "%DATA_DIR%\wallet:/Satori/Neuron/wallet" ^
        -v "%DATA_DIR%\models:/Satori/models" ^
        -v "%DATA_DIR%\data:/Satori/Engine/db" ^
        %IMAGE%

    if %errorlevel% neq 0 (
        echo Error: Failed to create container. Is Docker running?
        exit /b 1
    )
    exit /b 0

:help
echo Satori Neuron
echo.
echo Usage: satori [local] [instance] [command]
echo.
echo Modes:
echo   local       Use local Docker image (SATORI_ENV=dev, no pull)
echo.
echo Environment overrides:
echo   SATORI_FORCE_ENV   Force container SATORI_ENV (e.g. prod)
echo   SATORI_CENTRAL_URL Override central URL
echo   SATORI_API_URL     Alias override for central URL
echo   SATORI_DOCKER_SOCKET Override Docker socket path for relay sidecars
echo   DOCKER_HOST        Pass Docker endpoint through to the container relay manager
echo.
echo Managed relay ports:
echo   Public relay  base port: %SATORI_RELAY_PUBLIC_BASE_PORT%
echo   Private relay base port: %SATORI_RELAY_PRIVATE_BASE_PORT%
echo.
echo Instance:
echo   (number)    Instance number (default: 1)
echo.
echo Commands:
echo   (none)      Enter interactive CLI
echo   start       Start the neuron container
echo   stop        Stop the neuron container
echo   restart     Restart the neuron container
echo   logs        Show container logs
echo   status      Show container status
echo   --help      Show this help
echo.
echo Examples:
echo   satori              # Run instance 1 on port 24601 (pulls latest)
echo   satori local        # Run instance 1 with local latest image
echo   satori 2            # Run instance 2 on port 24602
echo   satori local 2      # Run instance 2 with local latest image
echo   satori 2 stop       # Stop instance 2
echo   satori local restart # Restart with local latest image
echo   satori 3 logs       # Show logs for instance 3
echo.
echo Data persists in: .\^<instance^>\config, wallet, models, data
exit /b 0

:start
docker start %CONTAINER_NAME%
echo Satori neuron %INSTANCE% started (port %PORT%)
exit /b 0

:stop
docker stop %CONTAINER_NAME% 2>nul
docker rm %CONTAINER_NAME% 2>nul
echo Satori neuron %INSTANCE% stopped and removed
exit /b 0

:restart
echo Restarting Satori neuron %INSTANCE%...
docker stop %CONTAINER_NAME% 2>nul
docker rm %CONTAINER_NAME% 2>nul
call :create_container
if %errorlevel% neq 0 exit /b 1
echo Satori neuron %INSTANCE% restarted with latest image (port %PORT%)
exit /b 0

:logs
docker logs %CONTAINER_NAME% --tail 100 -f
exit /b 0

:status
docker ps --format "{{.Names}}" | findstr /r "^%CONTAINER_NAME%$" >nul 2>&1
if %errorlevel% equ 0 (
    echo Satori neuron %INSTANCE% is running (port %PORT%)
    docker ps --filter "name=%CONTAINER_NAME%" --format "Container: {{.Names}}\nStatus: {{.Status}}\nCreated: {{.CreatedAt}}"
) else (
    echo Satori neuron %INSTANCE% is not running
)
exit /b 0

:cli
docker ps --format "{{.Names}}" | findstr /r "^%CONTAINER_NAME%$" >nul 2>&1
if %errorlevel% equ 0 goto :run_cli

echo Satori neuron %INSTANCE% is not running. Starting on port %PORT%...

docker start %CONTAINER_NAME% >nul 2>&1
if %errorlevel% equ 0 (
    timeout /t 2 /nobreak >nul
    goto :run_cli
)

echo Container not found. Creating...
call :create_container
if %errorlevel% neq 0 exit /b 1

timeout /t 2 /nobreak >nul

:run_cli
docker exec -it %CONTAINER_NAME% python /Satori/Neuron/cli.py
exit /b 0

:unknown
echo Unknown command: %CMD%
echo Run 'satori --help' for usage
exit /b 1
