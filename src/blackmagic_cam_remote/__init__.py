"""Blackmagic Cam Remote: a browser dashboard for the Blackmagic Camera app on an Android phone.

Modules:
  config      paths, constants, runtime options
  settings    settings kept between runs
  adb         finding the phone and running adb commands
  util        small networking helpers
  mirror      live view (screen video + microphone via scrcpy-server)
  screen      phone screen on/off while recording
  phone       phone health, rotation, viewfinder position
  automation  actions when recording starts/stops
  events      live camera state from the app's event websocket
  setup       setup-screen checks, pairing, connecting
  readiness   live connection / app-in-front state, pushed to the page
  recordings  clip list, playback/download, thumbnails
  auth        optional password login
  tls         HTTPS for the dashboard
  webserver   the HTTP server and its routes
"""
