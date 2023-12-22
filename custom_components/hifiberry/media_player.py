"""Hifiberry Platform."""
import logging
from datetime import timedelta
import asyncio
from contextlib import asynccontextmanager, suppress
from socket import gaierror
from typing import Any
import aiohttp

from homeassistant.components import media_source
from homeassistant.components.media_player import (
    MediaPlayerEntity,
    MediaPlayerEntityFeature,
    MediaType,
    async_process_play_media_url,
)
from homeassistant.components.media_player.const import (
    MEDIA_TYPE_MUSIC,
    SUPPORT_NEXT_TRACK,
    SUPPORT_PAUSE,
    SUPPORT_PLAY,
    SUPPORT_PREVIOUS_TRACK,
    SUPPORT_STOP,
    SUPPORT_VOLUME_MUTE,
    SUPPORT_VOLUME_SET,
    SUPPORT_VOLUME_STEP,
    SUPPORT_TURN_OFF,
    SUPPORT_PLAY_MEDIA,
)
from homeassistant.const import (
    STATE_IDLE,
    STATE_PAUSED,
    STATE_PLAYING,
)
from pyhifiberry.audiocontrol2 import Audiocontrol2Exception, LOGGER
from pyhifiberry.audiocontrol2sio import Audiocontrol2SIO

import mpd
from mpd.asyncio import MPDClient
import voluptuous as vol

from .const import DATA_HIFIBERRY, DATA_INIT, DOMAIN

SUPPORT_HIFIBERRY = (
     MediaPlayerEntityFeature.PAUSE
    |  MediaPlayerEntityFeature.VOLUME_SET
    |  MediaPlayerEntityFeature.VOLUME_MUTE
    |  MediaPlayerEntityFeature.PREVIOUS_TRACK
    |  MediaPlayerEntityFeature.NEXT_TRACK
    |  MediaPlayerEntityFeature.STOP
    |  MediaPlayerEntityFeature.PLAY
    |  MediaPlayerEntityFeature.VOLUME_STEP
    |  MediaPlayerEntityFeature.TURN_OFF
    |  MediaPlayerEntityFeature.PLAY_MEDIA
    |  MediaPlayerEntityFeature.MEDIA_ANNOUNCE
)

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass, config_entry, async_add_entities):
    """Set up the hifiberry media player platform."""

    data = hass.data[DOMAIN][config_entry.entry_id]
    audiocontrol2 = data[DATA_HIFIBERRY]
    uid = config_entry.entry_id
    name = f"hifiberry {config_entry.data['host']}"
    base_url = f"http://{config_entry.data['host']}:{config_entry.data['port']}"

    use_mpd = config_entry.data['use_mpd']
    mpd_host = config_entry.data['host']
    mpd_port = config_entry.data['mpd_port']
    mpd_password = config_entry.data['mpd_password']

    entity = HifiberryMediaPlayer(audiocontrol2, uid, name, base_url, use_mpd, mpd_host, mpd_port, mpd_password)
    async_add_entities([entity])


class HifiberryMediaPlayer(MediaPlayerEntity):
    """Hifiberry Media Player Object."""

    should_poll = False

    def __init__(self, audiocontrol2: Audiocontrol2SIO, uid, name, base_url, use_mpd, mpd_host, mpd_port, mpd_password):
        """Initialize the media player."""
        self._audiocontrol2 = audiocontrol2
        self._uid = uid
        self._name = name
        self._base_url = base_url
        self._muted = audiocontrol2.volume.percent == 0
        self._muted_volume = audiocontrol2.volume.percent

        self._mpd_host = mpd_host
        self._mpd_port = mpd_port
        self._mpd_password = mpd_password if mpd_password != "" else None

        self._mpd_is_available = None
        self._mpd_status = {}

        if use_mpd:
            self._mpd_client = MPDClient()
            self._mpd_client.timeout = 30
            self._mpd_client.idletimeout = 10
            self._mpd_client_lock = asyncio.Lock()
        else:
            self._mpd_client = None

    # Instead of relying on python-mpd2 to maintain a (persistent) connection to
    # MPD, the below explicitly sets up a *non*-persistent connection. This is
    # done to workaround the issue as described in:
    #   <https://github.com/Mic92/python-mpd2/issues/31>
    @asynccontextmanager
    async def mpd_connection(self):
        """Handle MPD connect and disconnect."""
        async with self._mpd_client_lock:
            try:
                # MPDClient.connect() doesn't always respect its timeout. To
                # prevent a deadlock, enforce an additional (slightly longer)
                # timeout on the coroutine itself.
                try:
                    async with asyncio.timeout(self._mpd_client.timeout + 5):
                        await self._mpd_client.connect(self._mpd_host, self._mpd_port)
                except asyncio.TimeoutError as error:
                    # TimeoutError has no message (which hinders logging further
                    # down the line), so provide one.
                    raise asyncio.TimeoutError(
                        "Connection attempt timed out"
                    ) from error
                if self._mpd_password is not None:
                    await self._mpd_client.password(self._mpd_password)
                self._mpd_is_available = True
                yield
            except (
                asyncio.TimeoutError,
                gaierror,
                mpd.ConnectionError,
                OSError,
            ) as error:
                # Log a warning during startup or when previously connected; for
                # subsequent errors a debug message is sufficient.
                log_level = logging.DEBUG
                if self._mpd_is_available is not False:
                    log_level = logging.WARNING
                _LOGGER.log(
                    log_level, "Error connecting to '%s': %s", self._mpd_host, error
                )
                self._mpd_is_available = False
                self._mpd_status = {}
                # Also yield on failure. Handling mpd.ConnectionErrors caused by
                # attempting to control a disconnected client is the
                # responsibility of the caller.
                yield
            finally:
                with suppress(mpd.ConnectionError):
                    self._mpd_client.disconnect()

    async def async_update(self):
        """Retrieve latest state."""
        if self._mpd_client is None:
            return
        async with self.mpd_connection():
            try:
                self._mpd_status = await self._mpd_client.status()
            except mpd.ConnectionError:
                self._mpd_status = {}
                return

    @property
    def available(self) -> bool:
        """Return true if device is responding."""
        return self._audiocontrol2.connected

    @property
    def unique_id(self):
        """Return the unique id for the entity."""
        return self._uid

    @property
    def device_info(self):
        """Return device info for this device."""
        return {
            "identifiers": {(DOMAIN, self.unique_id)},
            "name": self.name,
            "manufacturer": "Hifiberry",
        }

    async def async_added_to_hass(self) -> None:
        """Run when this Entity has been added to HA."""
        await self._audiocontrol2.volume.get()      ### make sure volume percent is set
        self._audiocontrol2.metadata.add_callback(self.schedule_update_ha_state)
        self._audiocontrol2.volume.add_callback(self.schedule_update_ha_state)

    async def async_will_remove_from_hass(self) -> None:
        """Entity being removed from hass."""
        await self._audiocontrol2.disconnect()

    @property
    def media_content_type(self):
        """Content type of current playing media."""
        return MEDIA_TYPE_MUSIC

    @property
    def state(self):
        """Return the state of the device."""
        status = self._audiocontrol2.metadata.playerState
        if status == "paused":
            return STATE_PAUSED
        if status == "playing":
            return STATE_PLAYING
        return STATE_IDLE

    @property
    def media_position_updated_at(self):
        """When was the position of the current playing media valid.

        Returns value from homeassistant.util.dt.utcnow().
        """
        return self._audiocontrol2.metadata.positionupdate

    @property
    def media_title(self):
        """Title of current playing media."""
        return self._audiocontrol2.metadata.title

    @property
    def media_artist(self):
        """Artist of current playing media (Music track only)."""
        return self._audiocontrol2.metadata.artist

    @property
    def media_album_name(self):
        """Artist of current playing media (Music track only)."""
        return self._audiocontrol2.metadata.albumTitle

    @property
    def media_album_artist(self):
        """Album artist of current playing media, music track only."""
        return self._audiocontrol2.metadata.albumArtist

    @property
    def media_track(self):
        """Track number of current playing media, music track only."""
        return self._audiocontrol2.metadata.tracknumber

    @property
    def media_image_url(self):
        """Image url of current playing media."""
        art_url = self._audiocontrol2.metadata.artUrl
        external_art_url = self._audiocontrol2.metadata.externalArtUrl
        if art_url is not None:
            if art_url.startswith("static/"):
                return external_art_url
            if art_url.startswith("artwork/"):
                return f"{self._base_url}/{art_url}"
            return art_url
        return external_art_url

    @property
    def volume_level(self):
        """Volume level of the media player (0..1)."""
        return int(self._audiocontrol2.volume.percent) / 100

    @property
    def is_volume_muted(self):
        """Boolean if volume is currently muted."""
        return self._muted

    @property
    def name(self):
        """Return the name of the device."""
        return self._name

    @property
    def source(self):
        """Name of the current input source."""
        return self._audiocontrol2.metadata.playerName

    @property
    def supported_features(self):
        """Flag of media commands that are supported."""
        return SUPPORT_HIFIBERRY

    async def async_media_next_track(self):
        """Send media_next command to media player."""
        await self._audiocontrol2.player.next()

    async def async_media_previous_track(self):
        """Send media_previous command to media player."""
        await self._audiocontrol2.player.previous()

    async def async_media_play(self):
        """Send media_play command to media player."""
        await self._audiocontrol2.player.play()

    async def async_media_pause(self):
        """Send media_pause command to media player."""
        await self._audiocontrol2.player.pause()

    async def async_volume_up(self):
        """Service to send the hifiberry the command for volume up."""
        await self._audiocontrol2.volume.set(min(100, self._audiocontrol2.volume.percent + 5))

    async def async_volume_down(self):
        """Service to send the hifiberry the command for volume down."""
        await self._audiocontrol2.volume.set(max(0, self._audiocontrol2.volume.percent - 5))

    async def async_set_volume_level(self, volume):
        """Send volume_set command to media player."""
        if volume < 0:
            volume = 0
        elif volume > 1:
            volume = 1
        await self._audiocontrol2.volume.set(int(volume * 100))
        self._volume = volume * 100

    async def async_mute_volume(self, mute):
        """Mute. Emulated with set_volume_level."""
        if mute:
            self._muted_volume = self.volume_level * 100
            await self._audiocontrol2.volume.set(0)
        await self._audiocontrol2.volume.set(int(self._muted_volume))
        self._muted = mute

    async def async_turn_off(self):
        return await self._audiocontrol2.poweroff()

    async def restore_source(self, source):
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self._base_url}/api/player/activate/org.mpris.MediaPlayer2.{source}") as response:
                if response.status != 200:
                    log_level = logging.DEBUG
                    _LOGGER.log(
                        log_level, "Error restoring source: %s", response.status
                    )
                return

    async def async_play_media(
        self,
        media_type: MediaType | str,
        media_id: str,
        announce: bool | None = None,
        **kwargs: Any
    ) -> None:

        async def wait_until_mpd_paused():
            playing = False
            stopped = False
            while not (playing and stopped):
                state = (await self._mpd_client.status()).get("state")
                print(state)
                if state == "play":
                    playing = True
                if playing:
                    if state != "play":
                        stopped = True
                await asyncio.sleep(0.1)

        """Send the media player the command for playing a playlist."""
        async with self.mpd_connection():
            if media_source.is_media_source_id(media_id):
                media_type = MediaType.MUSIC
                play_item = await media_source.async_resolve_media(
                    self.hass, media_id, self.entity_id
                )
                media_id = async_process_play_media_url(self.hass, play_item.url)

            if media_type == MediaType.PLAYLIST:
                _LOGGER.warning("Playlist is unsupported.")
            else:
                # announce = True
                need_state_restore = False
                if announce and self.state == STATE_PLAYING:
                    need_state_restore = True
                    last_source = self.source
                    last_volume = self.volume_level
                    await self.async_media_pause()
                    await self.async_set_volume_level(0.4)
                await self._mpd_client.clear()
                await self._mpd_client.add(media_id)
                await self._mpd_client.play()
                if need_state_restore:
                    await wait_until_mpd_paused()
                    await self.async_set_volume_level(last_volume)
                    await self.restore_source(last_source)
