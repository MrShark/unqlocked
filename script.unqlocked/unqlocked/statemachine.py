# *  This Program is free software; you can redistribute it and/or modify
# *  it under the terms of the GNU General Public License as published by
# *  the Free Software Foundation; either version 2, or (at your option)
# *  any later version.
# *
# *  This Program is distributed in the hope that it will be useful,
# *  but WITHOUT ANY WARRANTY; without even the implied warranty of
# *  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# *  GNU General Public License for more details.
# *
# *  You should have received a copy of the GNU General Public License
# *  along with XBMC; see the file COPYING.  If not, write to
# *  the Free Software Foundation, 675 Mass Ave, Cambridge, MA 02139, USA.
# *  http://www.gnu.org/copyleft/gpl.html

from unqlocked import log, createTruthMatrix, gcd, WINDOW_ID, Time
import solver

from copy import deepcopy
import datetime
import threading
import xbmc # for getCondVisibility()


class StateMachine(threading.Thread):
	'''
	Representation of a state machine, where the state is defined by a
	reference to an internal (fake) clock or an external (real) clock,
	and the only transition between states occurs as a transition between
	two different times.
	'''
	def __init__(self, delay):
		super(StateMachine, self).__init__()
		self._stop = False
		self.waitCondition = threading.Condition()
		self.delay = delay
		
		# Calculate the initial state from the current time
		now = datetime.datetime.now()
		seconds = now.hour * 60 * 60 + now.minute * 60 + now.second
		
		# Round the time down
		self.state = Time.fromSeconds(seconds / self.delay * self.delay)
		
		# When we first start the thread, the window might not be active yet.
		# Keep track of whether we sight the window; if it subsequently falls
		# off the map, we know we should exit
		self.windowSighted = False
	
	def run(self):
		self.waitCondition.acquire()
		while not self.shouldStop():
			# Allow the subclass to update the GUI
			log('StateMachine: visiting state ' + str(self.state))
			self.step(self.state)
			
			# Compute the next state
			next = (self.state.toSeconds() + self.delay) % (24 * 60 * 60)
			self.state = Time.fromSeconds(next)
			
			# Calculate the delay
			now = datetime.datetime.now()
			seconds = now.hour * 60 * 60 + now.minute * 60 + now.second + now.microsecond / 1000000.0
			delay = self.state.toSeconds() - seconds
			if delay < 0:
				delay = delay + 24 * 60 * 60
			log('Sleeping for %f seconds' % delay)
			self.waitCondition.wait(delay)
		#except:
		#	log('Exception thrown in StateMachine thread')
		#	self.waitCondition.release()
		#	self.stop()
		log('StateMachine shutting down')
		self.cleanup()
		self.waitCondition.release()
		log('StateMachine finished shutting down')
	
	def shouldStop(self):
		'''
		Two conditions result in stopping: a call to stop(), or the window not
		being visible after once being visible.
		'''
		visible = xbmc.getCondVisibility('Window.IsVisible(%s)' % WINDOW_ID)
		if self.windowSighted and not visible:
			return True
		elif visible:
			self.windowSighted = True
		return self._stop
	
	def stop(self):
		self.waitCondition.acquire()
		self._stop = True
		self.waitCondition.notifyAll()
		self.waitCondition.release()


class QlockThread(StateMachine):
	def __init__(self, window, layout):
		delay = self.calcDelay(layout)
		super(QlockThread, self).__init__(delay)
		self.window = window
		
		# Use a lowercase matrix for comparison
		self.layout = deepcopy(layout)
		for row in range(self.layout.height):
			for col in range(self.layout.width):
				self.layout.matrix[row][col] = self.layout.matrix[row][col].lower()
		
		log('Creating the solver')
		now = datetime.datetime.now()
		start = now.hour * 60 * 60 + now.minute * 60 + now.second + now.microsecond / 1000000.0
		
		# Let the solver know about the default delay. It will need this
		# information once it has parsed a times string into tokens.
		self.solver = solver.Solver(layout, delay)
		
		nodes = self.solver.countNodes()
		now = datetime.datetime.now()
		stop = now.hour * 60 * 60 + now.minute * 60 + now.second + now.microsecond / 1000000.0
		log('Solver created in %f seconds with %d nodes and %d rules' % (stop - start, nodes, len(layout.times)))
	
	def step(self, time):
		# Ask the solver for the time
		log('Solving for the current time (%s)' % str(time))
		solution = self.solver.resolveTime(time)
		solutionUTF8 = [uni.encode('utf-8') for uni in solution]
		log('Solution: ' + str(solutionUTF8))
		
		truthMatrix = createTruthMatrix(self.layout.height, self.layout.width)
		success = self.highlight(self.layout.matrix, truthMatrix, solution)
		if not success:
			log('Unable to highlight solution. Reattempting with no spaces between words')
			truthMatrix = createTruthMatrix(self.layout.height, self.layout.width)
			success = self.highlight(self.layout.matrix, truthMatrix, solution, False)
			if success:
				log('Success')
			else:
				log('Failed to highlight solution again. Drawing best attempt')
		
		# Draw the result
		self.window.drawMatrix(truthMatrix)
	
	def highlight(self, charMatrix, truthMatrix, tokens, forceSpace = True):
		'''
		Highlight tokens in truthMatrix as they are found in charMatrix.
		This function returns True if all tokens are highlighted sucessfully,
		and False otherwise.
		'''
		for row in range(self.layout.height):
			if not len(tokens):
				break
			consumed = self.highlightRow(charMatrix[row], row, truthMatrix, tokens, forceSpace)
			tokens = tokens[consumed:] # snip snip
		return len(tokens) == 0
	
	def highlightRow(self, charRow, row, truthMatrix, tokens, forceSpace = True):
		'''
		Highlight tokens in a row of chars (and each char is allowed to be
		composed of more than one char, such as in o'clock). row is used to
		keep track of the row in truthMatrix to highlight values on. The return
		value is the number of tokens consumed.
		
		forceSpace -- if True, this won't allow consecutive tokens to be
		highlighted.
		Ex: charRow = ['a','h','a','l','f'], tokens = ['a', 'half']
		Given charRow and tokens above, only the letter 'a' will be highlighted
		becase no space occurs between the two words. If 'half' does not occur
		AGAIN, the highlighting operation will not fully succeed.
		'''
		tokensCopy = tokens # Shallow copy (deep is done via slice later)
		# Use a while loop, because looping over a range doesn't let you
		# arbitrarily increment the loop counter
		i = 0
		while i < len(charRow):
			# No tokens means we're all done
			if len(tokens) == 0:
				break
			token = tokens[0]
			
			# Convert the (useful part of) charRow to a string for comparison
			if ''.join(charRow[i:]).startswith(token):
				# Found a match
				while len(token):
					truthMatrix[row][i] = True
					
					# Don't just pop 1 char, because charRow[i] might be longer
					# than a single char (such as the o' in o'clock)
					token = token[len(charRow[i]):]
					i = i + 1
				
				# If forceSpace is False, we need to allow consecutive words.
				# i was set to the end of the token, but we increment i as part
				# of the while loop, so the end result is that consecutive words
				# are automatically avoided. To reverse this, decrement i to
				# cancel out the while loop's increment (Post-mortem: we could
				# also just *continue*, but where's the fun in that?)
				if not forceSpace:
					i = i - 1
				
				# Elements have been highlighted, move on to the next token
				tokens = tokens[1:]
			i = i + 1 # Move on to the next token
		return len(tokensCopy) - len(tokens)
	
	def cleanup(self):
		'''Clear window properties'''
		truthMatrix = createTruthMatrix(self.layout.height, self.layout.width)
		self.window.drawMatrix(truthMatrix)
		pass
	
	def calcDelay(self, layout):
		'''The delay is calculated from the GCD of all time entries'''
		return reduce(gcd, [time.toSeconds() for time in layout.times.keys()])


# Not implemented yet
class SpriteThread(StateMachine):
	def __init__(self, window, config):
		super(SpriteThread, self).__init__(self.calcDelay(config))
		self.window = window
		self.config = config
	
	def step(self, time):
		# For default minute dots
		self.window.drawSprites(time.minutes % 5)
	
	def cleanup(self):
		# Clear window properties
		pass
	
	def calcDelay(self, config):
		# Read the type of sprites from self.config
		# If minute dots, return 60
		return 60 # seconds
