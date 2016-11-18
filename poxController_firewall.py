# Author: Mrinal Aich
# Roll No. CS16MTECH11009

"""
A Firewall based SDN Controller

It is derived from one written live for an l2_learning.py
"""

from pox.core import core
import pox.openflow.libopenflow_01 as of
from pox.lib.util import dpid_to_str
from pox.lib.util import str_to_bool
from pox.lib.packet.arp import arp
from pox.lib.packet.ipv4 import ipv4
from pox.lib.packet.ipv6 import ipv6
from pox.lib.addresses import IPAddr, EthAddr
import time

log = core.getLogger()

# We don't want to flood immediately when a switch connects.
# Can be overriden on commandline.
_flood_delay = 0

class LearningSwitch (object):
  """
  The learning switch "brain" associated with a single OpenFlow switch.

  When we see a packet, we'd like to output it on a port which will
  eventually lead to the destination.  To accomplish this, we build a
  table that maps addresses to ports.

  We populate the table by observing traffic.  When we see a packet
  from some source coming from some port, we know that source is out
  that port.

  When we want to forward traffic, we look up the desintation in our
  table.  If we don't know the port, we simply send the message out
  all ports except the one it came in on.  (In the presence of loops,
  this is bad!).

  In short, our algorithm looks like this:

  For each packet from the switch:
  0) Use source address, destination address and dst port to check in the firewall
     DONE
  1) Use source address and switch port to update address/port table
  2) Is transparent = False and either Ethertype is LLDP or the packet's
     destination address is a Bridge Filtered address?
     Yes:
        2a) Drop packet -- don't forward link-local traffic (LLDP, 802.1x)
            DONE
  3) Is destination multicast?
     Yes:
        3a) Flood the packet
            DONE
  4) Port for destination address in our address/port table?
     No:
        4a) Flood the packet
            DONE
  5) Is output port the same as input port?
     Yes:
        5a) Drop packet and similar ones for a while
  6) Install flow table entry in the switch so that this
     flow goes out the appopriate port
     6a) Send the packet out appropriate port
  """
  def __init__ (self, connection, transparent):
    # Switch we'll be adding L2 learning switch capabilities to
    self.connection = connection
    self.transparent = transparent

    # Our table
    self.macToPort = {}

    # Our firewall table in the form of Dictionary
    self.firewall = {}

    # Add a Couple of Rules Static entries
    # Two type of rules: (srcip,dstip) or (dstip,dstport)
    self.AddRule(dpid_to_str(connection.dpid), IPAddr('10.0.0.1'), IPAddr('10.0.0.4'),0)
    self.AddRule(dpid_to_str(connection.dpid), 0, IPAddr('10.0.0.3'), 80)

    # We want to hear PacketIn messages, so we listen
    # to the connection
    connection.addListeners(self)

    # We just use this to know when to log a helpful message
    self.hold_down_expired = _flood_delay == 0

  # function that allows adding firewall rules into the firewall table
  def AddRule (self, dpidstr, srcipstr, dstipstr, dstport,value=True):
      if dstport == 0:
        self.firewall[(dpidstr,srcipstr,dstipstr)] = True
        log.debug("Adding firewall rule of %s -> %s in %s", srcipstr, dstipstr, dpidstr)
      elif srcipstr == 0:
        self.firewall[(dpidstr,dstipstr,dstport)] = True
        log.debug("Adding firewall rule of Dst(%s,%s) in %s", dstipstr, dstport, dpidstr)
      else:
        self.firewall[(dpidstr,srcipstr,dstipstr,dstport)] = True
        log.debug("Adding firewall rule of %s -> %s,%s in %s", srcipstr, dstipstr, dstport, dpidstr)

  # function that allows deleting firewall rules from the firewall table
  def DeleteRule (self, dpidstr, srcipstr, dstipstr, dstport):
     try:
       if dstport == 0:
         del self.firewall[(dpidstr,srcipstr,dstipstr)]
         log.debug("Deleting firewall rule of %s -> %s in %s", srcipstr, dstipstr, dpidstr)
       elif srcipstr == 0:
         del self.firewall[(dpidstr,dstipstr,dstport)]
         log.debug("Deleting firewall rule of Dst(%s,%s) in %s", dstipstr, dstport, dpidstr)
       else:
         del self.firewall[(dpidstr,srcipstr,dstipstr,dstport)]
         log.debug("Deleting firewall rule of %s -> %s,%s in %s", srcipstr, dstipstr, dstport, dpidstr)
     except KeyError:
       log.error("Cannot find Rule %s -> %s,%s in %s", srcipstr, dstipstr, dstport, dpidstr)

  # check if packet is compliant to rules before proceeding
  def CheckRule (self, dpidstr, srcipstr, dstipstr, dstport):
    # Host to Host blocked
    try:
      entry = self.firewall[(dpidstr, srcipstr, dstipstr)]
      log.info("Rule (%s x->x %s) found in %s: DROP", srcipstr, dstipstr, dpidstr)
      return entry
    except KeyError:
      log.debug("Rule (%s -> %s) NOT found in %s: IP Rule NOT found", srcipstr, dstipstr, dpidstr)

    # Destination Process blocked
    try:
      entry = self.firewall[(dpidstr, dstipstr, dstport)]
      log.info("Rule Dst(%s,%s)) found in %s: DROP", dstipstr, dstport, dpidstr)
      return entry
    except KeyError:
      log.debug("Rule Dst(%s,%s) NOT found in %s: IP Rule NOT found", dstipstr, dstport, dpidstr)
      return False

  def _handle_PacketIn (self, event):
    """
    Handle packet in messages from the switch to implement above algorithm.
    """
    packet = event.parsed
    inport = event.port

    def flood (message = None):
      """ Floods the packet """
      msg = of.ofp_packet_out()
      if time.time() - self.connection.connect_time >= _flood_delay:
        # Only flood if we've been connected for a little while...

        if self.hold_down_expired is False:
          # Oh yes it is!
          self.hold_down_expired = True
          log.info("%s: Flood hold-down expired -- flooding", dpid_to_str(event.dpid))

        if message is not None: log.debug(message)
        msg.actions.append(of.ofp_action_output(port = of.OFPP_FLOOD))
      else:
        pass
        #log.info("Holding down flood for %s", dpid_to_str(event.dpid))
      msg.data = event.ofp
      msg.in_port = event.port
      self.connection.send(msg)

    def drop (duration = None):
      """
      Drops this packet and optionally installs a flow to continue
      dropping similar ones for a while
      """
      if duration is not None:
        if not isinstance(duration, tuple):
          duration = (duration,duration)
        msg = of.ofp_flow_mod()
        msg.match = of.ofp_match.from_packet(packet)
        msg.idle_timeout = duration[0]
        msg.hard_timeout = duration[1]
        msg.buffer_id = event.ofp.buffer_id
        self.connection.send(msg)
      elif event.ofp.buffer_id is not None:
        msg = of.ofp_packet_out()
        msg.buffer_id = event.ofp.buffer_id
        msg.in_port = event.port
        self.connection.send(msg)

    self.macToPort[packet.src] = event.port # 1

    # Get the DPID of the Switch Connection
    dpidstr = dpid_to_str(event.connection.dpid)
    #log.debug("Connection ID: %s" % dpidstr)

    if isinstance(packet.next, ipv4):
      log.debug("%i IP %s => %s", inport, packet.next.srcip,packet.next.dstip)
      segmant = packet.find('tcp')
      if segmant is not None:
        # Check the Firewall Rules in IPv4 and TCP Layer
        if self.CheckRule(dpidstr, packet.next.srcip, packet.next.dstip, segmant.dstport) == True:
          drop()
          return
      else:
        # Check the Firewall Rules in IPv4 Layer
        if self.CheckRule(dpidstr, packet.next.srcip, packet.next.dstip, 0) == True:
          drop()
          return
    elif isinstance(packet.next, arp):
      a = packet.next
      log.debug("%i ARP %s %s => %s", inport, {arp.REQUEST:"request",arp.REPLY:"reply"}.get(a.opcode, 'op:%i' % (a.opcode,)), str(a.protosrc), str(a.protodst))
    elif isinstance(packet.next, ipv6):
      # Do not consider ipv6 packets
      drop()
      return

    if not self.transparent: # 2
      if packet.type == packet.LLDP_TYPE or packet.dst.isBridgeFiltered():
        drop() # 2a
        return

    if packet.dst.is_multicast:
      flood() # 3a
    else:
      if packet.dst not in self.macToPort: # 4
        flood("Port for %s unknown -- flooding" % (packet.dst,)) # 4a
      else:
        port = self.macToPort[packet.dst]
        if port == event.port: # 5
          # 5a
          log.warning("Same port for packet from %s -> %s on %s.%s.  Drop." % (packet.src, packet.dst, dpid_to_str(event.dpid), port))
          drop(10)
          return
        # 6
        log.debug("installing flow for %s.%i -> %s.%i" % (packet.src, event.port, packet.dst, port))
        msg = of.ofp_flow_mod()
        msg.match = of.ofp_match.from_packet(packet, event.port)
        msg.idle_timeout = 10
        msg.hard_timeout = 30
        msg.actions.append(of.ofp_action_output(port = port))
        msg.data = event.ofp # 6a
        self.connection.send(msg)

class l2_learning (object):
  """
  Waits for OpenFlow switches to connect and makes them learning switches.
  """
  def __init__ (self, transparent):
    core.openflow.addListeners(self)
    self.transparent = transparent

  def _handle_ConnectionUp (self, event):
    log.debug("Connection %s" % (event.connection,))
    LearningSwitch(event.connection, self.transparent)


def launch (transparent=False, hold_down=_flood_delay):
  """
  Starts an L2 learning switch.
  """
  try:
    global _flood_delay
    _flood_delay = int(str(hold_down), 10)
    assert _flood_delay >= 0
  except:
    raise RuntimeError("Expected hold-down to be a number")

  core.registerNew(l2_learning, str_to_bool(transparent))
