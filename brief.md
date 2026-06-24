When it comes out, I have a picture that represents each encycical and its illustration color schem is a gradient as to how much of it is reflective vs prophetic

The concept is stored in [Encyiclals](Encyiclals)

The encyclals are in two categories. firstly - more spiritual, and concerning the matters of the church. the second is how the church choses to engage with the secular world. I'm interested in the latter.

Step 0: Find a way to document every single pope's encylical page and look at the number of publications. you should have a CSV indicating the pope, the published doc, title, publishing date, type of doc (pdf or raw text) and link to the doc

Step 1: write a program that can download all the encyclicals and strip them into text. if the encyclical is in PDF then first download, then parse it through a parser and break into clean markdown

Step 2: execute the program from step 1. you should have all pubs in simple markdown

Step 3: subagent pass 1 - choose a cheap model, and then have the subagent summarise the key themes in each paper. describe the classifiers but most importantly classify the ones with a secular context specifically, and in the others - infer if a secular context exists.

Step 4: into the CSV in step 1, add another column called themes, and put in the themes from step 3.

Step 5: for the ones that have a secular theme, run another sub agent, with a better model to understand and rank and critique the pub against a few clear specs such as  
social tension  
prescriptive towards?  
reflective towards?  
specific secular ideas mentioned?  
urgency, tone and weight

these are essentially textures I want to extract to used downstream

Step 6: use those textures to essentially build up a context map what major global events happened before and after the publishing of the piece. e.g if it talks about the industrial revolution, I might need to have a gradient of that topic from the date of publication to how relevant it remained as a social topic into the next year, 5 years, decade and decades.  
**note to self - need to decide if the temporal gradient is in single year increments (e.g year 1,2,3 etc) or grouped - 5 years, 10 years etc**

Step 7: Get to a score and understand how each individual encylical has been reflective or prescriptive and also rank how many of the popes got it right.

The temporal gradient leans into a color in 2 dimensions  
saturation - how strong the doc was reflectively or prescriptively  
density - how far along or ahead was the doc.

# Describing the artwork

The artwork needs to have a pipeline.

All starts with a single standard image - that has a strong z-depth pass. the z-depth gives the ability to make it 3 dimensional.

OR

The image is a single symbol describing the theme.

Potential styles  
subject and background having two diferent color themes

So let me think of a way to generate this.  
Each Paper will have a key symbolism or interpretation. I do this in the most bare minimal way.  
The gets used as a baseline to be turned into rich image with a good z depth pass. that gets a post processing filter applied.  
So the question is what is the framework for interpretation I will use

ok so the way it works is that I will - for each paper, draw on a piece of paper my internpretation. simple notebook and ballpoint pen. this is the seed data. then i will send it through a flora node flow that generates a z-depth pass image. that image is then fed into a geo node structure to generate more detailed geometry.  
then the geo node version and the z-depth pass are both sent to an ascii or dither post processor for the final image.  
the dither post processor works by breaking the image into big grid chunks. it looks at the overall saturation value. It then assigns a fill pattern to it, along with the gradient.

The fill patterns assigned to the image comes from the AI workflow. the AI workflow might say give it 10 blue dense blocks, 5 blue sparse blocks, and 15 green sparse blocks. (if blue is prescriptive and green is descriptive). Also if there are specific themes in each paper, it might warrant for a special few other colors in the image (orange, red, dark green etc)

Process

Spec this out  
find a way to acurately pull this context and planning into the git folder, and use hooks or other method to constantly sync between this and that

Maybe the other hook is to have a git sync on this folder route. More on this in [context issues](context issues)