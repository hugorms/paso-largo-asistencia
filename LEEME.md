# Paso Largo — control de asistencia con terminal facial (ISAPI)

Programa web para administrar terminales de reconocimiento facial Hikvision (serie DS-K1T / MinMoe)
por ISAPI y llevar el control de asistencia del personal.

## Cómo iniciarlo

Requisito: Python 3.10 o superior (en Windows, al instalarlo marca **"Add Python to PATH"**).

- **Windows:** doble clic en `iniciar.bat`. La primera vez instala lo necesario (1–2 minutos).
- **Linux / Mac:** `./iniciar.sh`

Luego abre **http://localhost:8000** — usuario `admin`, contraseña `admin123` (cámbiala en *Mi cuenta*).
Otras PCs de la misma red pueden entrar con `http://IP-DE-ESTA-PC:8000` (permite el puerto 8000 en el firewall).

La PC donde corre debe estar en la misma red que el terminal (por ejemplo 192.168.150.x) y quedar encendida,
porque es la que lee las marcaciones cada 10 segundos.

## Primeros pasos

1. **Dispositivos → Agregar equipo:** IP `192.168.150.231`, puerto 80, usuario `admin` y la contraseña del equipo.
   La hora y la zona horaria del equipo se toman de esta PC y se corrigen solas cada 10 minutos.
   Al guardar se prueba la conexión y se muestra modelo, serie y firmware.
2. **Horarios y ajustes:** crea horarios (entrada, salida, tolerancia, refrigerio, días laborables), departamentos y feriados.
3. **Empleados:** créalos con su foto (o con la cámara de la PC). Al guardar se envían al equipo con su rostro.
   Si ya tenías gente registrada en el equipo, usa **Importar → Desde un equipo**. También se puede importar un Excel.
4. Botón **Eventos** en el dispositivo: descarga el historial de marcaciones (por defecto 31 días).

## Qué incluye

- **Panel** en vivo: presentes, tardanzas, ausentes, gráfico de 7 días, últimas marcaciones, estado de equipos y botón de abrir puerta.
- **Asistencia del día** con entrada, salida, tardanza, horas trabajadas y estado; **marcas manuales** para olvidos (quedan auditadas).
- **Marcaciones**: todos los eventos del equipo con filtros y la foto capturada en cada marca.
- **Reportes** por rango, empleado o departamento: resumen (días, tardanzas, faltas, horas, horas extra) y detalle diario; **exportación a Excel y PDF**.
- **Permisos, vacaciones y licencias** que justifican faltas.
- **Varios equipos**: cada uno puede ser de entrada, salida o ambos.
- **Control de puerta**: abrir, mantener abierta, bloquear, volver a normal.
- **Sincronizar hora**, reiniciar equipo, probar conexión.
- **Usuarios con roles** (Administrador, Supervisor, Consulta) y **auditoría** de todas las acciones.

## Cómo se calcula la asistencia

- Entrada = primera marca del día; salida = última (si hay al menos 2 minutos de diferencia, configurable).
  Las marcas del medio (por ejemplo al almorzar) quedan en *Marcaciones* pero no cuentan.
- Si alguien marca una sola vez, las *ventanas* del horario dicen si fue entrada o salida.
  Las ventanas se mueven solas al cambiar la hora de entrada o salida del horario.
- Si el equipo está configurado como *solo entrada* o *solo salida*, o si el empleado elige Entrada/Salida en el terminal, se respeta
  (salvo que el equipo marque todo como "Entrada": entonces se ignora ese dato).
- Las marcas manuales se pueden borrar desde *Marcaciones* (queda en la auditoría).
- Tardanza = entrada después de *hora de inicio + tolerancia*. Se reportan los minutos desde la hora de inicio.
- El refrigerio se descuenta de las horas trabajadas cuando la jornada supera 5 horas.
- Falta = día laborable sin marcas, que no es feriado ni tiene permiso.
- Turnos que cruzan la medianoche no están soportados todavía.

## Datos

Todo se guarda en la carpeta `datos/` (base SQLite `asistencia.db` y fotos). **Haz copia de esa carpeta** para respaldar.
La contraseña del terminal se guarda en esa base, así que protege el acceso a la PC.

## Probar sin el equipo

`python simulador.py` levanta un terminal simulado en `127.0.0.1:8090` (usuario `admin`, clave `12345`)
que genera marcaciones al azar. Agrégalo como dispositivo para ver el sistema funcionando.

## Si cambias de red (el biométrico cambia de IP)

El sistema reconoce el biométrico por su **número de serie**, no solo por la IP:

- **Solo:** si el equipo deja de responder, a los ~20 segundos lo busca en la red (como la herramienta SADP de
  Hikvision y probando las IP de la red de esta PC). Si lo encuentra en otra IP, le actualiza la IP y vuelve a
  conectarse, sin crear otro equipo ni perder el historial. Mientras no aparezca, lo vuelve a buscar cada 2 minutos.
- **Botón "Buscar biométrico"** (en *Dispositivos*): busca al momento y muestra qué equipos hay en la red.
- **IP + "Probar y conectar"** (en cada equipo): si ya sabes la IP nueva, escríbela ahí. Solo se guarda si en esa IP
  responde el mismo equipo.

Si el equipo tiene IP fija de otra red (por ejemplo 192.168.1.x y la PC está en 192.168.150.x) la PC no puede
hablar con él: el sistema lo avisa. Ponle una IP de la red nueva con la herramienta SADP o activa DHCP en el equipo.

## Problemas comunes

- **401 / usuario o contraseña incorrectos:** revisa la clave; tras varios intentos fallidos el equipo bloquea la IP unos minutos.
- **No se pudo conectar:** verifica que la PC haga `ping 192.168.150.231` y que ISAPI/HTTP esté habilitado
  (en la web del equipo: *Configuración → Red → Servicios de red*, o en SADP).
- **Rostro rechazado:** la foto debe mostrar un solo rostro de frente, bien iluminado; prueba con otra foto.
- **Horas de las marcas desfasadas:** usa *Sincronizar hora* y revisa la zona horaria del dispositivo.

## Despliegue en Dokploy (Docker)

1. En Dokploy crea un servicio **Compose** (o **Application** con build type *Dockerfile*) apuntando a este repositorio, rama `main`.
2. En **Domains** asigna tu dominio al puerto **8000** (servicio `web`).
3. Despliega. Los datos (base de datos, fotos y clave de sesión) quedan en el volumen `paso-largo-datos` montado en `/data`, así que no se pierden al volver a desplegar.
4. Antes del primer despliegue define la variable `ADMIN_PASSWORD` en Dokploy (Environment) para no dejar `admin123` expuesto. Si no la defines, entra con `admin / admin123` y cámbiala de inmediato.

### Pasar los datos de la laptop al servidor
1. En la laptop: **Horarios y ajustes → Respaldo y migración → Descargar respaldo** (se descarga un .zip con base de datos y fotos).
2. Cierra Paso Largo en la laptop para que no siga leyendo el terminal.
3. En el servidor: entra como admin, **Horarios y ajustes → Restaurar**, elige el .zip. Luego entra con tus usuarios de siempre.
4. En **Dispositivos** actualiza la IP del terminal si cambió en la nueva ubicación.

Ese mismo botón sirve para sacar respaldos periódicos; además conviene activar los backups del volumen en Dokploy.

**Importante:** el servidor de Dokploy tiene que poder llegar al terminal Hikvision (por ejemplo `192.168.1.19`).
Si Dokploy está en un VPS en internet, no verá la red local del terminal: hace falta una VPN (WireGuard, Tailscale)
entre el VPS y la red del terminal, o instalar Dokploy en una PC/servidor dentro de esa misma red.

La zona horaria se define con la variable `TZ` (por defecto `America/Caracas`). Usa una sola réplica: el lector de eventos del equipo no debe duplicarse.

### Servidor del restaurante (192.168.1.40)

En Dokploy el servicio Compose usa el proveedor **Git** con `https://github.com/hugorms/paso-largo-asistencia.git`,
rama `main` y ruta `./servidor/docker-compose.dokploy.yml`. Los datos viven en `/home/paso_largo/paso-largo/datos`.
Para actualizar basta con hacer `git push` a `main`: el servidor revisa GitHub cada 2 minutos y despliega solo
(script `~/paso-largo-auto-deploy.sh` en cron; registro en `~/paso-largo-auto-deploy.log`).
