import ast
import datetime
import logging
import os
import sys
import urllib
import xml.dom.minidom
from urllib import FancyURLopener

import DICOMLib
import ctk
import qt
import slicer
import vtk
from DICOMLib import DICOMProcess
from constants import DICOMTAGS
from events import SlicerDevelopmentToolboxEvents
from mixins import ModuleLogicMixin, ModuleWidgetMixin, ParameterNodeObservationMixin


class SampleDataDownloader(FancyURLopener, ParameterNodeObservationMixin):

  EVENTS = {'status_changed': SlicerDevelopmentToolboxEvents.StatusChangedEvent,
            'download_canceled': SlicerDevelopmentToolboxEvents.DownloadCanceledEvent, # TODO: Implement cancel
            'download_finished': SlicerDevelopmentToolboxEvents.DownloadFinishedEvent,
            'download_failed': SlicerDevelopmentToolboxEvents.DownloadFailedEvent}

  def __init__(self, enableLogging=False):
    super(SampleDataDownloader, self).__init__()
    self.loggingEnabled = enableLogging
    self.isDownloading = False
    self.resetAndInitialize()

  def __del__(self):
    super(SampleDataDownloader, self).__del__()

  def resetAndInitialize(self):
    self._cancelDownload=False
    self.wasCanceled = False
    if self.isDownloading:
      self.cancelDownload()
    self.removeEventObservers()
    if self.loggingEnabled:
      self._addOwnObservers()

  def _addOwnObservers(self):
    for event in self.EVENTS.values():
      self.addEventObserver(event, self.logMessage)

  @vtk.calldata_type(vtk.VTK_STRING)
  def logMessage(self, caller, event, callData):
    message, _ = ast.literal_eval(callData)
    logging.debug(message)

  def downloadFileIntoCache(self, uri, name):
    return self.downloadFile(uri, slicer.mrmlScene.GetCacheManager().GetRemoteCacheDirectory(), name)

  def downloadFile(self, uri, destFolderPath, name):
    if self.isDownloading:
      self.cancelDownload()
    self._cancelDownload = False
    self.wasCanceled = False
    filePath = os.path.join(destFolderPath, name)
    if not os.path.exists(filePath) or os.stat(filePath).st_size == 0:
      self.downloadPercent = 0
      self.invokeEvent(self.EVENTS['status_changed'], ['Requesting download %s from %s...' % (name, uri),
                                                       self.downloadPercent].__str__())
      try:
        self.isDownloading = True
        self.retrieve(uri, filePath, self.reportHook)
        self.invokeEvent(self.EVENTS['status_changed'], ['Download finished', self.downloadPercent].__str__())
        # self.invokeEvent(self.EVENTS['download_finished'])
      except IOError as e:
        self.invokeEvent(self.EVENTS['download_failed'], ['Download failed: %s' % e, self.downloadPercent].__str__())
    else:
      self.invokeEvent(self.EVENTS['status_changed'], ['File already exists in cache - reusing it.', 100].__str__())
    return filePath

  def cancelDownload(self):
    self._cancelDownload=True

  def humanFormatSize(self, size):
    """ from http://stackoverflow.com/questions/1094841/reusable-library-to-get-human-readable-version-of-file-size"""
    for x in ['bytes', 'KB', 'MB', 'GB']:
      if -1024.0 < size < 1024.0:
        return "%3.1f %s" % (size, x)
      size /= 1024.0
    return "%3.1f %s" % (size, 'TB')

  def reportHook(self, blocksSoFar, blockSize, totalSize):
    percent = min(int((100. * blocksSoFar * blockSize) / totalSize), 100)
    humanSizeSoFar = self.humanFormatSize(min(blocksSoFar * blockSize, totalSize))
    humanSizeTotal = self.humanFormatSize(totalSize)
    self.downloadPercent = percent
    self.invokeEvent(self.EVENTS['status_changed'],
                     ['Downloaded %s (%d%% of %s)...' % (humanSizeSoFar, percent, humanSizeTotal),
                      self.downloadPercent].__str__())

  def retrieve(self, url, filename=None, reporthook=None, data=None):
    # overridden method from urllib.URLopener
    self._cancelDownload=False
    url = urllib.unwrap(urllib.toBytes(url))
    if self.tempcache and url in self.tempcache:
      return self.tempcache[url]
    type, url1 = urllib.splittype(url)
    if filename is None and (not type or type == 'file'):
      try:
        fp = self.open_local_file(url1)
        hdrs = fp.info()
        fp.close()
        return urllib.url2pathname(urllib.splithost(url1)[1]), hdrs
      except IOError:
        pass
    fp = self.open(url, data)
    try:
      headers = fp.info()
      if filename:
        tfp = open(filename, 'wb')
      else:
        import tempfile
        garbage, path = urllib.splittype(url)
        garbage, path = urllib.splithost(path or "")
        path, garbage = urllib.splitquery(path or "")
        path, garbage = urllib.splitattr(path or "")
        suffix = os.path.splitext(path)[1]
        (fd, filename) = tempfile.mkstemp(suffix)
        self.__tempfiles.append(filename)
        tfp = os.fdopen(fd, 'wb')
      try:
        result = filename, headers
        if self.tempcache is not None:
          self.tempcache[url] = result
        bs = 1024 * 8
        size = -1
        read = 0
        blocknum = 0
        if "content-length" in headers:
          size = int(headers["Content-Length"])
        if reporthook:
          reporthook(blocknum, bs, size)
        while not self._cancelDownload:
          block = fp.read(bs)
          if block == "":
            break
          read += len(block)
          tfp.write(block)
          blocknum += 1
          if reporthook:
            reporthook(blocknum, bs, size)
      finally:
        tfp.close()
    finally:
      fp.close()

    # raise exception if actual size does not match content-length header
    if size >= 0 and read < size:
      raise urllib.ContentTooShortError("retrieval incomplete: got only %i out "
                                 "of %i bytes" % (read, size), result)

    if self._cancelDownload and os.path.exists(filename):
      os.remove(filename)
      self.wasCanceled = True
    return result


class DirectoryWatcher(ModuleLogicMixin):

  StartedWatchingEvent = SlicerDevelopmentToolboxEvents.DICOMReceiverStartedEvent
  StoppedWatchingEvent = SlicerDevelopmentToolboxEvents.DICOMReceiverStoppedEvent
  IncomingFileCountChangedEvent = SlicerDevelopmentToolboxEvents.IncomingFileCountChangedEvent

  SUPPORTED_EVENTS = [StartedWatchingEvent, StoppedWatchingEvent, IncomingFileCountChangedEvent]

  def __init__(self, directory):
    self.observedDirectory = directory
    self.setupTimers()
    self.reset()

  def __del__(self):
    self.stop()
    super(DirectoryWatcher, self).__del__()

  def reset(self):
    self.startingFileList = []
    self.currentFileList = []
    self.currentStatus = ""
    self._running = False

  def isRunning(self):
    return self._running

  def setupTimers(self):
    self.watchTimer = self.createTimer(interval=1000, slot=self.startWatching, singleShot=True)

  def start(self):
    self.stop()
    self.startingFileList = self.getFileList(self.observedDirectory)
    self.lastFileCount = len(self.startingFileList)
    self._running = True
    self.startWatching()
    self.invokeEvent(self.StartedWatchingEvent)

  def stop(self):
    if self._running:
      self.watchTimer.stop()
      self.reset()
      self.invokeEvent(self.StoppedWatchingEvent)

  def startWatching(self):
    if not self.isRunning:
      return
    self.currentFileList = self.getFileList(self.observedDirectory)
    currentFileListCount = len(self.currentFileList)
    if self.lastFileCount != currentFileListCount:
      self.onFileCountChanged(currentFileListCount)
    else:
      self.watchTimer.start()

  def onFileCountChanged(self, currentFileListCount):
    self.lastFileCount = currentFileListCount
    receivedFileCount = abs(len(self.startingFileList) - currentFileListCount)
    self.invokeEvent(self.IncomingFileCountChangedEvent, receivedFileCount)
    self.watchTimer.start()


class TimeoutDirectoryWatcher(DirectoryWatcher):

  IncomingDataReceiveFinishedEvent = SlicerDevelopmentToolboxEvents.IncomingDataReceiveFinishedEvent

  SUPPORTED_EVENTS = DirectoryWatcher.SUPPORTED_EVENTS + [IncomingDataReceiveFinishedEvent]

  def __init__(self, directory, timeout=5000):
    self.receiveFinishedTimeout = timeout
    super(TimeoutDirectoryWatcher, self).__init__(directory)

  def setupTimers(self):
    super(TimeoutDirectoryWatcher, self).setupTimers()
    self.dataReceivedTimer = self.createTimer(interval=5000, slot=self.checkIfStillSameFileCount, singleShot=True)

  def stop(self):
    if self._running:
      self.dataReceivedTimer.stop()
    super(TimeoutDirectoryWatcher, self).stop()

  def startWatching(self):
    if not self.isRunning:
      return
    self.currentFileList = self.getFileList(self.observedDirectory)
    currentFileListCount = len(self.currentFileList)
    if self.lastFileCount != currentFileListCount:
      self.onFileCountChanged(currentFileListCount)
    elif currentFileListCount != len(self.startingFileList):
      self.lastFileCount = currentFileListCount
      self.dataReceivedTimer.start()
    else:
      self.watchTimer.start()

  def checkIfStillSameFileCount(self):
    self.currentFileList = self.getFileList(self.observedDirectory)
    if self.lastFileCount == len(self.currentFileList):
      newFileList = list(set(self.currentFileList) - set(self.startingFileList))
      self.startingFileList = self.currentFileList
      self.lastFileCount = len(self.startingFileList)
      if len(newFileList):
        self.invokeEvent(self.IncomingDataReceiveFinishedEvent, newFileList.__str__())
    self.watchTimer.start()


class SmartDICOMReceiver(ModuleLogicMixin):

  NAME = "SmartDICOMReceiver"
  STATUS_RECEIVING = "{}: Receiving DICOM data".format(NAME)

  StatusChangedEvent = SlicerDevelopmentToolboxEvents.StatusChangedEvent
  DICOMReceiverStartedEvent = SlicerDevelopmentToolboxEvents.DICOMReceiverStartedEvent
  DICOMReceiverStoppedEvent = SlicerDevelopmentToolboxEvents.DICOMReceiverStoppedEvent
  IncomingDataReceiveFinishedEvent = TimeoutDirectoryWatcher.IncomingDataReceiveFinishedEvent
  IncomingFileCountChangedEvent = TimeoutDirectoryWatcher.IncomingFileCountChangedEvent

  SUPPORTED_EVENTS = [DICOMReceiverStartedEvent, DICOMReceiverStoppedEvent, StatusChangedEvent,
                      IncomingDataReceiveFinishedEvent, IncomingFileCountChangedEvent]

  def __init__(self, incomingDataDirectory):
    self.incomingDataDirectory = incomingDataDirectory
    self.directoryWatcher = TimeoutDirectoryWatcher(incomingDataDirectory)
    self.connectEvents()
    self.storeSCPProcess = None
    self.reset()
    slicer.app.connect('aboutToQuit()', self.stop)

  def __del__(self):
    self.stop()
    super(SmartDICOMReceiver, self).__del__()

  def reset(self):
    self.currentStatus = ""
    self._running = False

  def connectEvents(self):
    self.directoryWatcher.addEventObserver(self.IncomingDataReceiveFinishedEvent, self.onDataReceivedFinished)
    self.directoryWatcher.addEventObserver(self.IncomingFileCountChangedEvent, self.onIncomingFileCountChanged)

  @vtk.calldata_type(vtk.VTK_STRING)
  def onDataReceivedFinished(self, caller, event, callData):
    self.updateStatus("{}: DICOM data receive completed.".format(self.NAME))
    self.invokeEvent(self.IncomingDataReceiveFinishedEvent, callData)

  @vtk.calldata_type(vtk.VTK_INT)
  def onIncomingFileCountChanged(self, caller, event, callData):
    status = "{}: Received {} files".format(self.NAME, callData)
    self.updateStatus(status)
    self.invokeEvent(self.IncomingFileCountChangedEvent, callData)

  def updateStatus(self, text):
    if text != self.currentStatus:
      self.currentStatus = text
      self.invokeEvent(self.StatusChangedEvent, text)

  def isRunning(self):
    return self._running

  def forceStatusChangeEvent(self):
    self.currentStatus = "Force update"

  def start(self, runStoreSCP=True):
    self.stop()
    self.directoryWatcher.start()
    if runStoreSCP:
      self.startStoreSCP()
    self.invokeEvent(self.DICOMReceiverStartedEvent)
    self._running = True
    self.updateStatus("{}: Waiting for incoming DICOM data".format(self.NAME) if self.storeSCPProcess else
                      "{}: Watching incoming data directory only (no storescp running)".format(self.NAME))

  def stop(self):
    if self._running:
      self.directoryWatcher.stop()
      self.stopStoreSCP()
      self.reset()
      self.invokeEvent(self.DICOMReceiverStoppedEvent)

  def startStoreSCP(self):
    self.stopStoreSCP()
    self.storeSCPProcess = DICOMLib.DICOMStoreSCPProcess(incomingDataDir=self.incomingDataDirectory)
    self.storeSCPProcess.start()

  def stopStoreSCP(self):
    if self.storeSCPProcess:
      self.storeSCPProcess.stop()
      self.storeSCPProcess = None


class SliceAnnotation(object):

  ALIGN_LEFT = "left"
  ALIGN_CENTER = "center"
  ALIGN_RIGHT = "right"
  ALIGN_TOP = "top"
  ALIGN_BOTTOM = "bottom"
  POSSIBLE_VERTICAL_ALIGN = [ALIGN_TOP, ALIGN_CENTER, ALIGN_BOTTOM]
  POSSIBLE_HORIZONTAL_ALIGN = [ALIGN_LEFT, ALIGN_CENTER, ALIGN_RIGHT]

  @property
  def fontSize(self):
    return self._fontSize

  @fontSize.setter
  def fontSize(self, size):
    self._fontSize = size
    if self.textProperty:
      self.textProperty.SetFontSize(self.fontSize)
      self.textActor.SetTextProperty(self.textProperty)
    self.update()

  @property
  def textProperty(self):
    if not self.textActor:
      return None
    return self.textActor.GetTextProperty()

  @textProperty.setter
  def textProperty(self, textProperty):
    assert issubclass(textProperty, vtk.vtkTextProperty)
    self.textActor.SetTextProperty(textProperty)
    self.update()

  @property
  def opacity(self):
    if self.textProperty:
      return self.textProperty.GetOpacity()
    return None

  @opacity.setter
  def opacity(self, value):
    if not self.textProperty:
      return
    self.textProperty.SetOpacity(value)
    self.update()

  @property
  def color(self):
    if self.textProperty:
      return self.textProperty.GetColor()

  @color.setter
  def color(self, value):
    assert type(value) is tuple and len(value) == 3
    if self.textProperty:
      self.textProperty.SetColor(value)
      self.update()

  @property
  def verticalAlign(self):
    return self._verticalAlign

  @verticalAlign.setter
  def verticalAlign(self, value):
    if value not in self.POSSIBLE_VERTICAL_ALIGN:
      raise ValueError("Value %s is not allowed for vertical alignment. Only the following values are allowed: %s"
                       % (str(value), str(self.POSSIBLE_VERTICAL_ALIGN)))
    else:
      self._verticalAlign = value

  @property
  def horizontalAlign(self):
    return self._horizontalAlign

  @horizontalAlign.setter
  def horizontalAlign(self, value):
    if value not in self.POSSIBLE_HORIZONTAL_ALIGN:
      raise ValueError("Value %s is not allowed for horizontal alignment. Only the following values are allowed: %s"
                       % (str(value), str(self.POSSIBLE_HORIZONTAL_ALIGN)))
    else:
      self._horizontalAlign = value

  @property
  def renderer(self):
    return self.sliceView.renderWindow().GetRenderers().GetItemAsObject(0)

  def __init__(self, widget, text, **kwargs):
    self.observer = None
    self.textActor = None
    self.text = text

    self.sliceWidget = widget
    self.sliceView = widget.sliceView()
    self.sliceLogic = widget.sliceLogic()
    self.sliceNode = self.sliceLogic.GetSliceNode()
    self.sliceNodeDimensions = self.sliceNode.GetDimensions()

    self.xPos = kwargs.pop('xPos', 0)
    self.yPos = kwargs.pop('yPos', 0)

    self.initialFontSize = kwargs.pop('fontSize', 20)
    self.fontSize = self.initialFontSize
    self.textColor = kwargs.pop('color', (1, 0, 0))
    self.textBold = kwargs.pop('bold', 1)
    self.textShadow = kwargs.pop('shadow', 1)
    self.textOpacity = kwargs.pop('opacity', 1.0)
    self.verticalAlign = kwargs.pop('verticalAlign', 'center')
    self.horizontalAlign = kwargs.pop('horizontalAlign', 'center')

    self.createTextActor()

  def show(self):
    self.fitIntoViewport()
    self._addActor()
    self._addObserver()
    self.sliceView.update()

  def hide(self):
    self.remove()

  def remove(self):
    self._removeObserver()
    self._removeActor()
    self.sliceView.update()

  def _addObserver(self):
    if not self.observer and self.sliceNode:
      self.observer = self.sliceNode.AddObserver(vtk.vtkCommand.ModifiedEvent, self.modified)

  def _removeObserver(self):
    if self.observer:
      self.sliceNode.RemoveObserver(self.observer)
      self.observer = None

  def _removeActor(self):
    try:
      self.renderer.RemoveActor(self.textActor)
      self.update()
    except:
      pass

  def _addActor(self):
    self.renderer.AddActor(self.textActor)
    self.update()

  def update(self):
    self.sliceView.update()

  def createTextActor(self):
    self.textActor = vtk.vtkTextActor()
    self.textActor.SetInput(self.text)
    self.textProperty.SetFontSize(self.fontSize)
    self.textProperty.SetColor(self.textColor)
    self.textProperty.SetBold(self.textBold)
    self.textProperty.SetShadow(self.textShadow)
    self.textProperty.SetOpacity(self.textOpacity)
    self.textActor.SetTextProperty(self.textProperty)
    self.show()

  def applyPositioning(self):
    xPos = self.applyHorizontalAlign()
    yPos = self.applyVerticalAlign()
    self.textActor.SetDisplayPosition(xPos, yPos)

  def applyHorizontalAlign(self):
    centerX = int((self.sliceView.width - self.getFontWidth()) / 2)
    if self.xPos:
      xPos = self.xPos if 0 < self.xPos < centerX else centerX
    else:
      if self.horizontalAlign == self.ALIGN_LEFT:
        xPos = 0
      elif self.horizontalAlign == self.ALIGN_CENTER:
        xPos = centerX
      elif self.horizontalAlign == self.ALIGN_RIGHT:
        xPos = self.sliceView.width - self.getFontWidth()
    return int(xPos)

  def applyVerticalAlign(self):
    centerY = int((self.sliceView.height - self.getFontHeight()) / 2)
    if self.yPos:
      yPos = self.yPos if 0 < self.yPos < centerY else centerY
    else:
      if self.verticalAlign == self.ALIGN_TOP:
        yPos = self.sliceView.height - self.getFontHeight()
      elif self.verticalAlign == self.ALIGN_CENTER:
        yPos = centerY
      elif self.verticalAlign == self.ALIGN_BOTTOM:
        yPos = 0
    return int(yPos)

  def modified(self, observee, event):
    if event != "ModifiedEvent":
      return
    currentDimensions = observee.GetDimensions()
    if currentDimensions != self.sliceNodeDimensions:
      self.fitIntoViewport()
      self.update()
      self.sliceNodeDimensions = currentDimensions

  def getFontWidth(self):
    return self.getFontDimensions()[0]

  def getFontHeight(self):
    return self.getFontDimensions()[1]

  def getFontDimensions(self):
    size = [0.0, 0.0]
    self.textActor.GetSize(self.renderer, size)
    return size

  def fitIntoViewport(self):
    while self.getFontWidth() < self.sliceView.width and self.fontSize < self.initialFontSize:
      self.fontSize += 1
    while self.getFontWidth() > self.sliceView.width:
      self.fontSize -= 1
    self.applyPositioning()


class ExtendedQMessageBox(qt.QMessageBox):

  def __init__(self, parent= None):
    super(ExtendedQMessageBox, self).__init__(parent)
    self.setupUI()

  def setupUI(self):
    self.checkbox = qt.QCheckBox("Remember the selection and do not notify again")
    self.layout().addWidget(self.checkbox, 1, 1)

  def exec_(self, *args, **kwargs):
    return qt.QMessageBox.exec_(self, *args, **kwargs), self.checkbox.isChecked()


class IncomingDataMessageBox(ExtendedQMessageBox):

  def __init__(self, parent=None):
    super(IncomingDataMessageBox, self).__init__(parent)
    self.setWindowTitle("Incoming image data")
    self.textLabel = qt.QLabel("New data has been received. What do you want do?")
    self.layout().addWidget(self.textLabel, 0, 1)
    self.setIcon(qt.QMessageBox.Question)
    trackButton = self.addButton(qt.QPushButton('Track targets'), qt.QMessageBox.AcceptRole)
    self.addButton(qt.QPushButton('Postpone'), qt.QMessageBox.NoRole)
    self.setDefaultButton(trackButton)

class IncomingDataWindow(qt.QWidget, ModuleWidgetMixin):

  def __init__(self, incomingDataDirectory, title="Receiving image data",
               skipText="Skip", cancelText="Cancel", *args):
    super(IncomingDataWindow, self).__init__(*args)
    self.setWindowTitle(title)
    self.setWindowFlags(qt.Qt.CustomizeWindowHint | qt.Qt.WindowTitleHint | qt.Qt.WindowStaysOnTopHint)
    self.skipButtonText = skipText
    self.cancelButtonText = cancelText
    self.setup()
    self.dicomReceiver = SmartDICOMReceiver(incomingDataDirectory=incomingDataDirectory)
    self.dicomReceiver.addEventObserver(self.dicomReceiver.StatusChangedEvent, self.onStatusChanged)
    self.dicomReceiver.addEventObserver(self.dicomReceiver.IncomingDataReceiveFinishedEvent, self.onReceiveFinished)
    self.dicomReceiver.addEventObserver(self.dicomReceiver.IncomingFileCountChangedEvent, self.onReceivingData)
    self.dicomSender = None

  def __del__(self):
    super(IncomingDataWindow, self).__del__()
    if self.dicomReceiver:
      self.dicomReceiver.removeEventObservers()

  @vtk.calldata_type(vtk.VTK_STRING)
  def onStatusChanged(self, caller, event, callData):
    self.textLabel.text = callData

  @vtk.calldata_type(vtk.VTK_INT)
  def onReceivingData(self, caller, event, callData):
    self.skipButton.enabled = False
    self.directoryImportButton.enabled = False

  def show(self, disableWidget=None):
    self.disabledWidget = disableWidget
    if disableWidget:
      disableWidget.enabled = False
    qt.QWidget.show(self)
    self.dicomReceiver.start()

  def hide(self):
    if self.disabledWidget:
      self.disabledWidget.enabled = True
      self.disabledWidget = None
    qt.QWidget.hide(self)
    self.dicomReceiver.stop()

  def setup(self):
    self.setLayout(qt.QGridLayout())
    self.statusLabel = qt.QLabel("Status:")
    self.textLabel = qt.QLabel()
    self.layout().addWidget(self.statusLabel, 0, 0)
    self.layout().addWidget(self.textLabel, 0, 1, 1, 2)

    self.progress = qt.QProgressBar()
    self.progress.maximum = 0
    self.progress.setAlignment(qt.Qt.AlignCenter)

    self.layout().addWidget(self.progress, 1, 0, 1, qt.QSizePolicy.Maximum)

    self.buttonGroup = qt.QButtonGroup()
    self.skipButton = self.createButton(self.skipButtonText)
    self.cancelButton = self.createButton(self.cancelButtonText)
    self.directoryImportButton = self.createDirectoryButton(text="Import from directory",
                                                            caption="Choose directory to import DICOM data from")

    self.buttonGroup.addButton(self.skipButton)
    self.buttonGroup.addButton(self.cancelButton)
    self.layout().addWidget(self.skipButton, 2, 0)
    self.layout().addWidget(self.cancelButton, 2, 1)
    self.layout().addWidget(self.directoryImportButton, 2, 2)

    buttonHeight = 30
    for b in [self.skipButton, self.cancelButton, self.directoryImportButton]:
      b.minimumHeight = buttonHeight

    self.setupConnections()

  def setupConnections(self):
    self.buttonGroup.connect('buttonClicked(QAbstractButton*)', self.onButtonClicked)
    self.directoryImportButton.directorySelected.connect(self.onImportDirectorySelected)

  def onButtonClicked(self, button):
    self.hide()
    if button is self.skipButton:
      self.invokeEvent(SlicerDevelopmentToolboxEvents.IncomingDataSkippedEvent)
    else:
      self.invokeEvent(SlicerDevelopmentToolboxEvents.IncomingDataCanceledEvent)
      if self.dicomSender:
        self.dicomSender.stop()

  def onReceiveFinished(self, caller, event):
    self.hide()
    self.invokeEvent(SlicerDevelopmentToolboxEvents.IncomingDataReceiveFinishedEvent)

  def onImportDirectorySelected(self, directory):
    self.dicomSender = DICOMDirectorySender(directory, 'localhost', 11112)


class DICOMDirectorySender(DICOMProcess):
  """Code to send files/directories to a remote host (uses storescu from dcmtk)
  """

  STORESCU_PROCESS_FILE_NAME = "storescu"


  def __init__(self, directory, address, port, progressCallback=None):
    super(DICOMDirectorySender, self).__init__()
    self.directory = directory
    self.address = address
    self.port = port
    self.progressCallback = progressCallback
    if not self.progressCallback:
      self.progressCallback = self.defaultProgressCallback
    self.send()

    self.storescuExecutable = os.path.join(self.exeDir, self.STORESCU_PROCESS_FILE_NAME + self.exeExtension)

  def __del__(self):
    super(DICOMDirectorySender,self).__del__()

  def onStateChanged(self, newState):
    stdout, stderr = super(DICOMDirectorySender, self).onStateChanged(newState)
    if stderr and stderr.size():
      slicer.util.errorDisplay("An error occurred. For further information click 'Show Details...'",
                               windowTitle=self.__class__.__name__, detailedText=str(stderr))
    return stdout, stderr

  def defaultProgressCallback(self,s):
    print(s)

  def send(self):
    self.progressCallback("Starting send to %s:%s" % (self.address, self.port))
    self.start()
    self.progressCallback("Sent %s to %s:%s" % (self.directory, self.address, self.port))

  def start(self, cmd=None, args=None):
    self.storeSCUExecutable = os.path.join(self.exeDir, 'storescu'+self.exeExtension)
    # TODO: check pattern,,, .DS_Store exclusion
    args = [str(self.address), str(self.port), "-aec", "CTK", "--scan-directories", "--recurse", "--scan-pattern" ,
            "*[0-9a-Z]", self.directory]
    super(DICOMDirectorySender,self).start(self.storeSCUExecutable, args)
    self.process.connect('readyReadStandardOutput()', self.readFromStandardOutput)

  def readFromStandardOutput(self, readLineCallback=None):
    print('================ready to read stdout from %s===================' % self.__class__.__name__)
    while self.process.canReadLine():
      line = str(self.process.readLine())
      print("From %s: %s" % (self.__class__.__name__, line))
      if readLineCallback:
        readLineCallback(line)
    print('================end reading stdout from %s===================' % self.__class__.__name__)
    self.readFromStandardError()

  def readFromStandardError(self):
    stdErr = str(self.process.readAllStandardError())
    if stdErr:
      print('================ready to read stderr from %s===================' % self.__class__.__name__)
      print ("processed stderr: %s" %stdErr)
      print('================end reading stderr from %s===================' % self.__class__.__name__)


class RatingWindow(qt.QWidget, ModuleWidgetMixin, ParameterNodeObservationMixin):

  @property
  def maximumValue(self):
    return self._maximumValue

  @maximumValue.setter
  def maximumValue(self, value):
    if value < 1:
      raise ValueError("The maximum rating value cannot be less than 1.")
    else:
      self._maximumValue = value

  def __init__(self, maximumValue, text="Please rate the registration result:", *args):
    qt.QWidget.__init__(self, *args)
    self.maximumValue = maximumValue
    self.text = text
    self.iconPath = os.path.join(os.path.dirname(sys.modules[self.__module__].__file__), '../Resources/Icons')
    self.setupIcons()
    self.setLayout(qt.QGridLayout())
    self.setWindowFlags(qt.Qt.WindowStaysOnTopHint | qt.Qt.FramelessWindowHint)
    self.setupElements()
    self.connectButtons()
    self.showRatingValue = True

  def __del__(self):
    super(RatingWindow, self).__del__()
    self.disconnectButtons()

  def isRatingEnabled(self):
    return not self.disableWidgetCheckbox.checked

  def setupIcons(self):
    self.filledStarIcon = self.createIcon("icon-star-filled.png", self.iconPath)
    self.unfilledStarIcon = self.createIcon("icon-star-unfilled.png", self.iconPath)

  def show(self, disableWidget=None):
    self.disabledWidget = disableWidget
    if disableWidget:
      disableWidget.enabled = False
    qt.QWidget.show(self)
    self.ratingScore = None

  def setupElements(self):
    self.layout().addWidget(qt.QLabel(self.text), 0, 0)
    self.ratingButtonGroup = qt.QButtonGroup()
    for rateValue in range(1, self.maximumValue+1):
      attributeName = "button"+str(rateValue)
      setattr(self, attributeName, self.createButton('', icon=self.unfilledStarIcon))
      self.ratingButtonGroup.addButton(getattr(self, attributeName), rateValue)

    for button in list(self.ratingButtonGroup.buttons()):
      button.setCursor(qt.Qt.PointingHandCursor)

    self.ratingLabel = self.createLabel("")
    row = self.createHLayout(list(self.ratingButtonGroup.buttons()) + [self.ratingLabel])
    self.layout().addWidget(row, 1, 0)

    self.disableWidgetCheckbox = qt.QCheckBox("Don't display this window again")
    self.disableWidgetCheckbox.checked = False
    self.layout().addWidget(self.disableWidgetCheckbox, 2, 0)

  def connectButtons(self):
    self.ratingButtonGroup.connect('buttonClicked(int)', self.onRatingButtonClicked)
    for button in list(self.ratingButtonGroup.buttons()):
      button.installEventFilter(self)

  def disconnectButtons(self):
    self.ratingButtonGroup.disconnect('buttonClicked(int)', self.onRatingButtonClicked)
    for button in list(self.ratingButtonGroup.buttons()):
      button.removeEventFilter(self)

  def eventFilter(self, obj, event):
    if obj in list(self.ratingButtonGroup.buttons()) and event.type() == qt.QEvent.HoverEnter:
      self.onHoverEvent(obj)
    elif obj in list(self.ratingButtonGroup.buttons()) and event.type() == qt.QEvent.HoverLeave:
      self.onLeaveEvent()
    return qt.QWidget.eventFilter(self, obj, event)

  def onLeaveEvent(self):
    for button in list(self.ratingButtonGroup.buttons()):
      button.icon = self.unfilledStarIcon

  def onHoverEvent(self, obj):
    ratingValue = 0
    for button in list(self.ratingButtonGroup.buttons()):
      button.icon = self.filledStarIcon
      ratingValue += 1
      if obj is button:
        break
    if self.showRatingValue:
      self.ratingLabel.setText(str(ratingValue))

  def onRatingButtonClicked(self, buttonId):
    self.ratingScore = buttonId
    if self.disabledWidget:
      self.disabledWidget.enabled = True
      self.disabledWidget = None
    self.invokeEvent(SlicerDevelopmentToolboxEvents.RatingWindowClosedEvent, str(self.ratingScore))
    self.hide()


class WatchBoxAttribute(object):

  MASKED_PLACEHOLDER = "X"
  TRUNCATE_LENGTH = None

  @property
  def title(self):
    return self.titleLabel.text

  @title.setter
  def title(self, value):
    self.titleLabel.text = value if value else ""

  @property
  def masked(self):
    return self._masked

  @masked.setter
  def masked(self, value):
    if self._masked == value:
      return
    self._masked = value
    self.updateVisibleValues(self.originalValue if not self.masked else self.maskedValue(self.originalValue))

  @property
  def value(self):
    return self.valueLabel.text

  @value.setter
  def value(self, value):
    self.originalValue = str(value) if value else ""
    self.updateVisibleValues(self.originalValue if not self.masked else self.maskedValue(self.originalValue))

  @property
  def originalValue(self):
    return self._value

  @originalValue.setter
  def originalValue(self, value):
    self._value = value

  def __init__(self, name, title, tags=None, masked=False, callback=None):
    self.name = name
    self._masked = masked
    self.titleLabel = qt.QLabel()
    self.titleLabel.setStyleSheet("QLabel{ font-weight: bold;}")
    self.valueLabel = qt.QLabel()
    self.title = title
    self.callback = callback
    self.tags = None if not tags else tags if type(tags) is list else [str(tags)]
    self.value = None

  def updateVisibleValues(self, value):
    self.valueLabel.text = value[0:self.TRUNCATE_LENGTH]+"..." if self.TRUNCATE_LENGTH and \
                                                                  len(value) > self.TRUNCATE_LENGTH else value
    self.valueLabel.toolTip = value

  def maskedValue(self, value):
    return self.MASKED_PLACEHOLDER * len(value)


class BasicInformationWatchBox(qt.QGroupBox):

  DEFAULT_STYLE = 'background-color: rgb(230,230,230)'
  PREFERRED_DATE_FORMAT = "%Y-%b-%d"

  def __init__(self, attributes, title="", parent=None, columns=1):
    super(BasicInformationWatchBox, self).__init__(title, parent)
    self.attributes = attributes
    self.columns = columns
    if not self.checkAttributeUniqueness():
      raise ValueError("Attribute names are not unique.")
    self.setup()

  def checkAttributeUniqueness(self):
    onlyNames = [attribute.name for attribute in self.attributes]
    return len(self.attributes) == len(set(onlyNames))

  def reset(self):
    for attribute in self.attributes:
      attribute.value = ""

  def setup(self):
    self.setStyleSheet(self.DEFAULT_STYLE)
    layout = qt.QGridLayout()
    self.setLayout(layout)

    column = 0
    for index, attribute in enumerate(self.attributes):
      layout.addWidget(attribute.titleLabel, index/self.columns, column*2, 1, 1, qt.Qt.AlignLeft)
      layout.addWidget(attribute.valueLabel, index/self.columns, column*2+1, 1, qt.Qt.AlignLeft)
      column = column+1 if column<self.columns-1 else 0

  def getAttribute(self, name):
    for attribute in self.attributes:
      if attribute.name == name:
        return attribute
    return None

  def setInformation(self, attributeName, value, toolTip=None):
    attribute = self.getAttribute(attributeName)
    attribute.value = value
    attribute.valueLabel.toolTip = toolTip

  def getInformation(self, attributeName):
    attribute = self.getAttribute(attributeName)
    return attribute.value if not attribute.masked else attribute.originalValue

  def formatDate(self, dateToFormat):
    if dateToFormat and dateToFormat != "":
      formatted = datetime.date(int(dateToFormat[0:4]), int(dateToFormat[4:6]), int(dateToFormat[6:8]))
      return formatted.strftime(self.PREFERRED_DATE_FORMAT)
    return "No Date found"

  def formatPatientName(self, name):
    if name != "":
      splitted = name.split('^')
      try:
        name = splitted[1] + ", " + splitted[0]
      except IndexError:
        name = splitted[0]
    return name


class FileBasedInformationWatchBox(BasicInformationWatchBox):

  DEFAULT_TAG_VALUE_SEPARATOR = ": "
  DEFAULT_TAG_NAME_SEPARATOR = "_"

  @property
  def sourceFile(self):
    return self._sourceFile

  @sourceFile.setter
  def sourceFile(self, filePath):
    self._sourceFile = filePath
    if not filePath:
      self.reset()
    self.updateInformation()

  def __init__(self, attributes, title="", sourceFile=None, parent=None, columns=1):
    super(FileBasedInformationWatchBox, self).__init__(attributes, title, parent, columns)
    if sourceFile:
      self.sourceFile = sourceFile

  def _getTagNameFromTagNames(self, tagNames):
    return self.DEFAULT_TAG_NAME_SEPARATOR.join(tagNames)

  def _getTagValueFromTagValues(self, values):
    return self.DEFAULT_TAG_VALUE_SEPARATOR.join(values)

  def updateInformation(self):
    for attribute in self.attributes:
      if attribute.callback:
        value = attribute.callback()
      else:
        value = self.updateInformationFromWatchBoxAttribute(attribute)
      self.setInformation(attribute.name, value, toolTip=value)

  def updateInformationFromWatchBoxAttribute(self, attribute):
    raise NotImplementedError


class XMLBasedInformationWatchBox(FileBasedInformationWatchBox):

  DATE_TAGS_TO_FORMAT = ["StudyDate", "PatientBirthDate", "SeriesDate", "ContentDate", "AcquisitionDate"]

  @FileBasedInformationWatchBox.sourceFile.setter
  def sourceFile(self, filePath):
    self._sourceFile = filePath
    if filePath:
      self.dom = xml.dom.minidom.parse(self._sourceFile)
    else:
      self.reset()
    self.updateInformation()

  def __init__(self, attributes, title="", sourceFile=None, parent=None, columns=1):
    super(XMLBasedInformationWatchBox, self).__init__(attributes, title, sourceFile, parent, columns)

  def reset(self):
    super(XMLBasedInformationWatchBox, self).reset()
    self.dom = None

  def updateInformationFromWatchBoxAttribute(self, attribute):
    if attribute.tags and self.dom:
      values = []
      for tag in attribute.tags:
        currentValue = ModuleLogicMixin.findElement(self.dom, tag)
        if tag in self.DATE_TAGS_TO_FORMAT:
          currentValue = self.formatDate(currentValue)
        elif tag == "PatientName":
          currentValue = self.formatPatientName(currentValue)
        values.append(currentValue)
      return self._getTagValueFromTagValues(values)
    return ""


class DICOMBasedInformationWatchBox(FileBasedInformationWatchBox):

  DATE_TAGS_TO_FORMAT = [DICOMTAGS.STUDY_DATE, DICOMTAGS.PATIENT_BIRTH_DATE]

  def __init__(self, attributes, title="", sourceFile=None, parent=None, columns=1):
    super(DICOMBasedInformationWatchBox, self).__init__(attributes, title, sourceFile, parent, columns)

  def updateInformationFromWatchBoxAttribute(self, attribute):
    if attribute.tags and self.sourceFile:
      values = []
      for tag in attribute.tags:
        currentValue = ModuleLogicMixin.getDICOMValue(self.sourceFile, tag, "")
        if tag in self.DATE_TAGS_TO_FORMAT:
          currentValue = self.formatDate(currentValue)
        elif tag == DICOMTAGS.PATIENT_NAME:
          currentValue = self.formatPatientName(currentValue)
        values.append(currentValue)
      return self._getTagValueFromTagValues(values)
    return ""


class SettingsMessageBox(qt.QMessageBox, ModuleWidgetMixin):

  def getSettingNames(self):
    return [s.replace(self.moduleName+"/", "") for s in list(qt.QSettings().allKeys()) if str.startswith(str(s),
                                                                                                         self.moduleName)]

  def __init__(self, moduleName, parent=None, **kwargs):
    self.moduleName = moduleName
    self.keyElementPairs = []
    qt.QMessageBox.__init__(self, parent, **kwargs)
    self.setup()
    self.adjustSize()

  def setup(self):
    self.setLayout(qt.QGridLayout())
    settingNames = self.getSettingNames()
    for index, setting in enumerate(settingNames):
      label = self.createLabel(setting)
      value = self.getSetting(setting)
      if value.lower() in ["true", "false"]:
        element = qt.QCheckBox()
        element.checked = value.lower() == "true"
      elif value.isdigit():
        element = qt.QSpinBox()
        element.value = int(value)
      elif os.path.exists(value):
        element = ctk.ctkPathLineEdit()
        if os.path.isdir(value):
          element.filters = ctk.ctkPathLineEdit.Dirs
        else:
          element.filters = ctk.ctkPathLineEdit.Files
        element.currentPath = value
      else:
        element = self.createLineEdit(value)
        element.minimumWidth = self.getMinimumTextWidth(element.text) + 10

      self.layout().addWidget(label, index, 0)
      self.layout().addWidget(element, index, 1, 1, qt.QSizePolicy.ExpandFlag)
      self.keyElementPairs.append((label.text, element))

    self.okButton = self.createButton("OK")
    self.cancelButton = self.createButton("Cancel")

    self.addButton(self.okButton, qt.QMessageBox.AcceptRole)
    self.addButton(self.cancelButton, qt.QMessageBox.NoRole)

    self.layout().addWidget(self.createHLayout([self.okButton, self.cancelButton]), len(settingNames), 1)
    self.okButton.clicked.connect(self.onOkButtonClicked)

  def getMinimumTextWidth(self, text):
    font = qt.QFont("", 0)
    metrics = qt.QFontMetrics(font)
    return metrics.width(text)

  def onOkButtonClicked(self):
    for key, element in self.keyElementPairs:
      if isinstance(element, qt.QCheckBox):
        value = "true" if element.checked else "false"
      elif isinstance(element, qt.QSpinBox):
        value = str(element.value)
      elif isinstance(element, ctk.ctkPathLineEdit):
        value = element.currentPath
      else:
        value = element.text
      if self.getSetting(key) != value:
        self.setSetting(key, value)
    self.close()